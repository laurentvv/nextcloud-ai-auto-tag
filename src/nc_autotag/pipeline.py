import asyncio
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

import httpx

from nc_autotag.config import Settings
from nc_autotag.inference.d1 import D1Client, TagResult
from nc_autotag.media import dedupe
from nc_autotag.media.images import to_jpeg
from nc_autotag.media.ssh import Fetched, SftpSource, SkipFile
from nc_autotag.media.video import extract_frames, probe_duration, probe_pixels
from nc_autotag.nextcloud.client import NcClient, NcUnavailable, NextcloudError
from nc_autotag.nextcloud.tags import TagClient
from nc_autotag.nextcloud.webdav import RemoteFile
from nc_autotag.state import Assignment, FileRow, State

VIDEO_FRAMES = 6
QUEUE_SIZE = 2
NO_LIMIT = 10**9


class InferenceUnavailable(Exception):
    pass


@dataclass
class Prepared:
    kind: str
    jpeg: bytes | None
    frames: list[bytes] | None
    duration: float | None
    sig: dedupe.Signature


def describe_error(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:200]}"


def sync_inventory(state: State, user: str, files: list[RemoteFile]) -> Counter:
    counts: Counter = Counter()
    for f in files:
        counts["videos" if f.mimetype.startswith("video/") else "images"] += 1
        counts[
            state.upsert_file(user, f.fileid, f.path, f.mimetype, f.etag, f.size, commit=False)
        ] += 1
    state.db.commit()
    return counts


def prepare(
    fetched: Fetched, name: str, is_video: bool, max_side: int, frames: int = VIDEO_FRAMES
) -> Prepared:
    if is_video:
        duration = probe_duration(fetched.path)
        shots = extract_frames(fetched.path, frames, max_side, duration)
        sig = dedupe.video_signature(
            fetched.path, shots[len(shots) // 2], probe_pixels(fetched.path), name
        )
        return Prepared("video", None, shots, duration, sig)
    jpeg = to_jpeg(fetched.data, max_side)
    return Prepared("image", jpeg, None, None, dedupe.image_signature(fetched.data, jpeg, name))


def emitted_scores(d1: D1Client, result: TagResult) -> dict[str, float]:
    """Tags Nextcloud émis (hors « à supprimer ») avec le meilleur score des options retenues."""
    out: dict[str, float] = {}
    for name, score in result.selected.items():
        for tag in d1.taxonomy.emitted([name], d1.delete_tag):
            if tag != d1.delete_tag:
                out[tag] = max(out.get(tag, 0.0), score)
    return out


def store_result(
    state: State, f: FileRow, prep: Prepared, result: TagResult, d1: D1Client, run_id: int, ver: str
) -> None:
    state.record_scores(f.user, f.fileid, result.scores)
    state.record_proposals(f.user, f.fileid, emitted_scores(d1, result))
    state.set_result(f.user, f.fileid, result.reasons, run_id)
    sig = prep.sig
    state.upsert_hash(
        f.user,
        f.fileid,
        prep.kind,
        sig.dhash,
        sig.sharpness,
        sig.pixels,
        prep.duration,
        sig.taken_at,
    )
    state.mark_done(f.user, f.fileid, ver)


async def process_files(
    state: State,
    source: SftpSource,
    d1: D1Client,
    files: list[FileRow],
    run_id: int,
    model_ver: str,
    max_side: int,
    frames: int = VIDEO_FRAMES,
    on_item: Callable[[FileRow, str, str | None], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Counter:
    """SFTP + prétraitement en avance (producteur) ; une seule inférence à la fois (GPU).

    should_stop : vérifié avant de prendre chaque nouveau fichier ; ceux déjà préparés sont finis,
    les autres restent en attente.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    counts: Counter = Counter()

    async def producer() -> None:
        for f in files:
            if should_stop and should_stop():
                break
            is_video = (f.mimetype or "").startswith("video/")
            try:
                if f.mimetype == "image/svg+xml":
                    raise SkipFile("format non pris en charge (svg)")
                fetched = await source.fetch(f.user, f.path, is_video)
                try:
                    name = PurePosixPath(f.path).name
                    prep = await asyncio.to_thread(
                        prepare, fetched, name, is_video, max_side, frames
                    )
                finally:
                    fetched.cleanup()
                item = (f, prep, None)
            except Exception as e:
                item = (f, None, e)
            await queue.put(item)
        await queue.put(None)

    def finish(f: FileRow, status: str, detail: str | None = None) -> None:
        counts[status] += 1
        if on_item:
            on_item(f, status, detail)

    task = asyncio.create_task(producer())
    try:
        while (item := await queue.get()) is not None:
            f, prep, err = item
            if isinstance(err, SkipFile):
                state.mark_skipped(f.user, f.fileid, str(err))
                finish(f, "skipped", str(err))
            elif err is not None:
                state.mark_error(f.user, f.fileid, describe_error(err))
                finish(f, "error", describe_error(err))
            else:
                try:
                    if prep.kind == "video":
                        result = await d1.tag_video(prep.frames, prep.duration)
                    else:
                        result = await d1.tag_image(prep.jpeg)
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    raise InferenceUnavailable(
                        "llama-server injoignable : fichiers restants laissés en attente"
                    ) from e
                except Exception as e:
                    state.mark_error(f.user, f.fileid, describe_error(e))
                    finish(f, "error", describe_error(e))
                    continue
                store_result(state, f, prep, result, d1, run_id, model_ver)
                finish(f, "done")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return counts


def detect_duplicates(state: State, settings: Settings, users: list[str]) -> int:
    """Marque « doublon » les copies de chaque compte (le meilleur exemplaire est gardé)."""
    total = 0
    for user in users:
        groups = dedupe.find_duplicate_groups(
            state.get_hashes(user),
            settings.quality.dup_max_hamming,
            settings.quality.dup_window_minutes,
        )
        flags = {fileid: keeper for (_, fileid), keeper in dedupe.duplicate_flags(groups).items()}
        state.apply_duplicates(user, dedupe.REASON, flags)
        total += len(flags)
    return total


def wanted_tags(
    state: State, f: FileRow, delete_tag: str, max_tags: int, with_delete: bool = True
) -> list[str]:
    """Tags voulus : « à supprimer » d'abord s'il y a des raisons, puis par score décroissant."""
    tags = [t for t in state.get_proposals(f.user, f.fileid) if t != delete_tag]
    if f.reasons and with_delete:
        tags.insert(0, delete_tag)
    return tags[:max_tags]


def wanted_key(wanted: list[str]) -> str:
    return json.dumps(sorted(wanted), ensure_ascii=False)


def files_to_apply(
    state: State,
    settings: Settings,
    users: list[str],
    path_prefix: str | None = None,
    limit: int | None = None,
    exclude_paths: list[str] | None = None,
    with_delete: bool = True,
) -> list[tuple[FileRow, list[str]]]:
    """Fichiers traités dont les tags voulus diffèrent de ceux déjà appliqués."""
    out = []
    for user in users:
        for f in state.list_files(user, path_prefix, ("done",), exclude_paths=exclude_paths or ()):
            wanted = wanted_tags(
                state, f, settings.delete_tag_name, settings.max_tags_per_file, with_delete
            )
            if f.applied != wanted_key(wanted):
                out.append((f, wanted))
                if limit is not None and len(out) >= limit:
                    return out
    return out


class TagWriter:
    """Un TagClient par compte (ses identifiants), création des tags manquants une seule fois."""

    def __init__(self, make_client: Callable[[str], NcClient]):
        self.make_client = make_client
        self.clients: dict[str, NcClient] = {}
        self.tags: dict[str, TagClient] = {}
        self.created: set[str] = set()

    def for_user(self, user: str) -> TagClient:
        if user not in self.tags:
            client = self.make_client(user)
            self.clients[user] = client
            self.tags[user] = TagClient(client)
        return self.tags[user]

    async def ensure(self, tc: TagClient, name: str) -> int:
        if not tc.by_id:
            await tc.list_tags()
        existing = tc.find(name)
        if existing is not None:
            return existing
        before = set(tc.assignable)
        tag_id = await tc.ensure_tag(name)
        if name.casefold() not in before:
            self.created.add(name)
        return tag_id

    async def aclose(self) -> None:
        for c in self.clients.values():
            await c.aclose()


async def apply_one(
    state: State,
    writer: TagWriter,
    f: FileRow,
    wanted: list[str],
    run_id: int,
    counts: Counter,
    keep: frozenset[str] = frozenset(),
) -> None:
    tc = writer.for_user(f.user)
    current = await tc.file_tags(f.fileid)
    for name in wanted:
        if name.casefold() in current:
            if not any(
                a.tag.casefold() == name.casefold()
                for a in state.active_assignments(f.user, f.fileid)
            ):
                counts["preexisting"] += 1
            continue
        tag_id = await writer.ensure(tc, name)
        if await tc.assign(f.fileid, tag_id):
            state.journal_assignment(run_id, f.user, f.fileid, tc.name_of(tag_id) or name, tag_id)
            counts["added"] += 1
        else:
            counts["preexisting"] += 1
    protected = {w.casefold() for w in wanted} | {k.casefold() for k in keep}
    for a in state.active_assignments(f.user, f.fileid):
        if a.tag.casefold() in protected:
            continue
        tag_id = a.tag_id if a.tag_id is not None else tc.find(a.tag)
        if tag_id is None:
            raise NextcloudError(f"Tag « {a.tag} » introuvable pour le retrait")
        await tc.unassign(f.fileid, tag_id)
        state.mark_removed([a])
        counts["removed"] += 1
    state.set_applied(f.user, f.fileid, wanted)


async def apply_files(
    state: State,
    items: list[tuple[FileRow, list[str]]],
    run_id: int,
    make_client: Callable[[str], NcClient],
    on_item: Callable[[FileRow, str], None] | None = None,
    on_error: Callable[[FileRow, str], None] | None = None,
    keep: frozenset[str] = frozenset(),
) -> Counter:
    """Pose les tags manquants (journalisés) et retire les tags journalisés devenus inutiles.

    keep : tags journalisés à ne jamais retirer même s'ils ne sont pas voulus (--no-delete-tag).
    """
    counts: Counter = Counter()
    writer = TagWriter(make_client)
    try:
        for f, wanted in items:
            try:
                await apply_one(state, writer, f, wanted, run_id, counts, keep)
            except NcUnavailable:
                raise
            except NextcloudError as e:
                counts["errors"] += 1
                if on_error:
                    on_error(f, describe_error(e))
                continue
            counts["files"] += 1
            if on_item:
                on_item(f, "applied")
        counts["tags_created"] = len(writer.created)
    finally:
        await writer.aclose()
    return counts


async def rollback_assignments(
    state: State,
    assignments: list[Assignment],
    make_client: Callable[[str], NcClient],
) -> Counter:
    """Retire uniquement les affectations journalisées, avec les identifiants de leur compte."""
    counts: Counter = Counter()
    writer = TagWriter(make_client)
    try:
        for user in sorted({a.user for a in assignments}):
            tc = writer.for_user(user)
            await tc.list_tags()
            for a in (x for x in assignments if x.user == user):
                tag_id = a.tag_id if a.tag_id is not None else tc.find(a.tag)
                if tag_id is None:
                    counts["errors"] += 1
                    continue
                try:
                    removed = await tc.unassign(a.fileid, tag_id)
                except NcUnavailable:
                    raise
                except NextcloudError:
                    counts["errors"] += 1
                    continue
                state.mark_removed([a])
                state.clear_applied(a.user, a.fileid)
                counts["removed" if removed else "already_gone"] += 1
    finally:
        await writer.aclose()
    return counts


@dataclass
class RunReport:
    run_id: int
    mode: str
    counts: Counter = field(default_factory=Counter)
    to_add: Counter = field(default_factory=Counter)
    delete: list[FileRow] = field(default_factory=list)
    apply_counts: Counter | None = None
    duplicates: int = 0
    pending_apply: int = 0
    stopped: bool = False
    remaining: int = 0
    problems: list[tuple[FileRow, str, str | None]] = field(default_factory=list)


async def run(
    settings: Settings,
    state: State,
    d1: D1Client,
    source: SftpSource,
    users: list[str],
    *,
    path_prefix: str | None = None,
    limit: int | None = None,
    apply: bool = False,
    reprocess: bool = False,
    max_side: int | None = None,
    exclude_paths: list[str] | None = None,
    no_delete_tag: bool = False,
    stop_at: datetime | None = None,
    clock: Callable[[], datetime] = datetime.now,
    make_client: Callable[[str], NcClient] | None = None,
    on_item: Callable[[FileRow, str, str | None], None] | None = None,
    on_apply_item: Callable[[FileRow, str], None] | None = None,
    on_apply_error: Callable[[FileRow, str], None] | None = None,
    on_plan: Callable[[RunReport, list], None] | None = None,
) -> RunReport:
    mode = "apply" if apply else "dry-run"
    run_id = state.start_run(mode)
    report = RunReport(run_id, mode)
    try:
        if reprocess:
            for user in users:
                state.requeue(user, path_prefix)
        files: list[FileRow] = []
        for user in users:
            room = NO_LIMIT if limit is None else limit - len(files)
            if room > 0:
                files += state.next_pending(room, user, path_prefix, exclude_paths or ())

        def track(f: FileRow, status: str, detail: str | None) -> None:
            if status != "done":
                report.problems.append((f, status, detail))
            if on_item:
                on_item(f, status, detail)

        timed_out: list[bool] = []

        def should_stop() -> bool:
            if stop_at is not None and clock() >= stop_at:
                timed_out.append(True)
                return True
            return False

        report.counts = await process_files(
            state,
            source,
            d1,
            files,
            run_id,
            settings.llama.model.stem,
            max_side or settings.llama.image_max_side,
            on_item=track,
            should_stop=should_stop,
        )
        report.remaining = sum(
            len(state.next_pending(NO_LIMIT, user, path_prefix, exclude_paths or ()))
            for user in users
        )
        report.stopped = bool(timed_out)
        report.duplicates = detect_duplicates(state, settings, users)
        for user in users:
            for f in state.list_files(user, statuses=("done",), run_id=run_id):
                wanted = wanted_tags(state, f, settings.delete_tag_name, settings.max_tags_per_file)
                report.to_add.update(wanted)
                if f.reasons:
                    report.delete.append(f)
        todo = files_to_apply(
            state, settings, users, path_prefix, limit, exclude_paths, not no_delete_tag
        )
        report.pending_apply = len(todo)
        if apply and not report.stopped:
            if on_plan:
                on_plan(report, todo)
            report.apply_counts = await apply_files(
                state,
                todo,
                run_id,
                make_client,
                on_apply_item,
                on_apply_error,
                frozenset([settings.delete_tag_name]) if no_delete_tag else frozenset(),
            )
    finally:
        stats = dict(report.counts)
        if report.apply_counts is not None:
            stats["apply"] = dict(report.apply_counts)
        state.finish_run(run_id, stats)
    return report
