import asyncio
import csv
import json
import mimetypes
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from nc_autotag import labels, pipeline
from nc_autotag.config import load_settings
from nc_autotag.doctor import CHECKS, FAIL, OK, run_checks
from nc_autotag.inference.d1 import D1Client, TagResult
from nc_autotag.media import dedupe
from nc_autotag.media.images import to_jpeg
from nc_autotag.media.ssh import SftpSource
from nc_autotag.media.video import extract_frames, probe_duration, probe_pixels
from nc_autotag.nextcloud import webdav
from nc_autotag.nextcloud.client import NcClient, NextcloudError
from nc_autotag.secrets import get_app_password
from nc_autotag.state import STATUSES, State
from nc_autotag.taxonomy import DEFAULT_TAGS, load_taxonomy

app = typer.Typer(help="Tags automatiques IA pour Nextcloud.", no_args_is_help=True)
console = Console()

_now = datetime.now

STYLES = {OK: "green", FAIL: "red"}


@app.callback()
def main() -> None:
    """Tags automatiques IA pour Nextcloud."""


@app.command()
def doctor(
    only: Annotated[
        list[str] | None,
        typer.Option("--only", help=f"Limiter aux vérifications : {', '.join(CHECKS)}"),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
) -> None:
    """Vérifie l'environnement (identifiants, WebDAV, SSH, llama.cpp, ffmpeg)."""
    unknown = [n for n in only or [] if n not in CHECKS]
    if unknown:
        console.print(f"[red]Vérification inconnue : {', '.join(unknown)}[/red]")
        raise typer.Exit(2)
    settings = load_settings(config)
    results = run_checks(settings, only)
    for r in results:
        style = STYLES.get(r.status, "yellow")
        console.print(
            f"[{style}]{r.status:<4}[/{style}] {r.name:<22} {escape(r.message)}",
            markup=True,
            highlight=False,
        )
    if any(r.status == FAIL for r in results):
        raise typer.Exit(1)


def _is_video(path: Path) -> bool:
    return (mimetypes.guess_type(path.name)[0] or "").startswith("video/")


async def _describe(paths: list[Path], client: D1Client, max_side: int, frames: int):
    for path in paths:
        t0 = time.perf_counter()
        try:
            if _is_video(path):
                duration = await asyncio.to_thread(probe_duration, path)
                data = await asyncio.to_thread(extract_frames, path, frames, max_side, duration)
                prep = time.perf_counter() - t0
                result = await client.tag_video(data, duration)
            else:
                data = await asyncio.to_thread(to_jpeg, str(path), max_side)
                prep = time.perf_counter() - t0
                result = await client.tag_image(data)
        except Exception as e:
            yield path, None, 0.0, 0.0, e
            continue
        yield path, result, prep, time.perf_counter() - t0, None


def _client(taxonomy, settings) -> D1Client:
    return D1Client(
        taxonomy,
        port=settings.llama.port,
        blur_var_threshold=settings.quality.blur_var_threshold,
        delete_tag=settings.delete_tag_name,
    )


def _print_result(
    path: Path, result: TagResult, prep: float, total: float, top: int, delete_tag: str
) -> None:
    unit = f"{result.frames} images" if result.frames > 1 else "1 image"
    console.print(
        f"[bold]{escape(path.name)}[/bold]  {unit}  préparation {prep:.2f}s  "
        f"inférence {result.latency:.2f}s  total {total:.2f}s",
        highlight=False,
    )
    table = Table("tag", "score", "retenu")
    ranked = sorted(result.scores.items(), key=lambda kv: -kv[1])
    for tag, score in ranked[:top]:
        kept = tag in result.selected
        table.add_row(escape(tag), f"{score:.3f}", "[green]oui[/green]" if kept else "")
    console.print(table)
    tags = ", ".join(escape(t) for t in result.emitted if t != delete_tag) or "(aucun)"
    console.print(f"Tags retenus : {tags}", highlight=False)
    if result.delete:
        why = ", ".join(result.reasons)
        console.print(
            f"[bold red]« {escape(delete_tag)} » : OUI (proposition, rien n'est supprimé) "
            f"- raisons : {why}[/bold red]\n",
            highlight=False,
        )
    else:
        console.print(f"« {escape(delete_tag)} » : non\n", highlight=False)


@app.command("describe-local")
def describe_local(
    paths: Annotated[list[Path], typer.Argument(help="Images ou vidéos locales")],
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
    tags: Annotated[Path, typer.Option("--tags", help="Chemin de tags.yaml")] = DEFAULT_TAGS,
    top: Annotated[int, typer.Option("--top", help="Nombre de scores affichés")] = 10,
    frames: Annotated[int, typer.Option("--frames", help="Images extraites par vidéo")] = 6,
    max_side: Annotated[
        int | None, typer.Option("--max-side", help="Côté max en pixels (défaut : settings.toml)")
    ] = None,
) -> None:
    """Passe des fichiers locaux dans le pipeline (sans toucher Nextcloud)."""
    missing = [p for p in paths if not p.is_file()]
    if missing:
        console.print(f"[red]Fichier introuvable : {escape(str(missing[0]))}[/red]")
        raise typer.Exit(2)
    settings = load_settings(config)
    taxonomy = load_taxonomy(tags)

    async def run() -> int:
        client = _client(taxonomy, settings)
        errors = 0
        try:
            async for path, result, prep, total, err in _describe(
                paths, client, max_side or settings.llama.image_max_side, frames
            ):
                if err is not None:
                    errors += 1
                    console.print(f"[red]{escape(path.name)} : {escape(str(err))}[/red]")
                else:
                    _print_result(path, result, prep, total, top, settings.delete_tag_name)
        finally:
            await client.aclose()
        return errors

    if asyncio.run(run()):
        raise typer.Exit(1)


LABELS_README = """Fichier créé : {out} (scores par tag : {scores})
Remplis la colonne true_tags avec le même vocabulaire, séparé par des points-virgules :
  {vocab}
Écris « - » pour une photo sans aucun tag ; une ligne vide est ignorée par « eval ».
Les scores des photos filtrées par la porte « {gate} » valent 0 pour les autres tags.
Puis lance : nc-autotag eval {out}"""


@app.command("make-labels")
def make_labels(
    directory: Annotated[Path, typer.Argument(help="Dossier d'images et vidéos", metavar="DIR")],
    out: Annotated[Path, typer.Option("--out", help="CSV de sortie")] = Path("labels.csv"),
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
    tags: Annotated[Path, typer.Option("--tags", help="Chemin de tags.yaml")] = DEFAULT_TAGS,
    frames: Annotated[int, typer.Option("--frames", help="Images extraites par vidéo")] = 6,
    max_side: Annotated[
        int | None, typer.Option("--max-side", help="Côté max en pixels (défaut : settings.toml)")
    ] = None,
) -> None:
    """Prédit les tags d'un dossier et écrit un CSV à corriger à la main (true_tags)."""
    if not directory.is_dir():
        console.print(f"[red]Dossier introuvable : {escape(str(directory))}[/red]")
        raise typer.Exit(2)
    paths = labels.scan_media(directory)
    if not paths:
        console.print("[red]Aucune image ou vidéo trouvée.[/red]")
        raise typer.Exit(2)
    settings = load_settings(config)
    taxonomy = load_taxonomy(tags)

    async def run() -> tuple[list, int]:
        client = _client(taxonomy, settings)
        items, errors = [], 0
        try:
            async for path, result, _, _, err in _describe(
                paths, client, max_side or settings.llama.image_max_side, frames
            ):
                if err is not None:
                    errors += 1
                    console.print(f"[red]{escape(path.name)} : {escape(str(err))}[/red]")
                else:
                    scores = {**result.scores, settings.delete_tag_name: float(result.delete)}
                    items.append((str(path), scores, result.emitted, result.reasons))
        finally:
            await client.aclose()
        return items, errors

    items, errors = asyncio.run(run())
    labels.write_labels(out, items)
    vocab = ";".join(taxonomy.vocabulary(settings.delete_tag_name))
    gate = taxonomy.gate.id if taxonomy.gate else "-"
    print(LABELS_README.format(out=out, scores=labels.scores_path(out), vocab=vocab, gate=gate))
    if errors:
        raise typer.Exit(1)


@app.command("eval")
def eval_labels(
    csv_path: Annotated[Path, typer.Argument(help="CSV rempli par make-labels", metavar="CSV")],
    tags: Annotated[Path, typer.Option("--tags", help="Chemin de tags.yaml")] = DEFAULT_TAGS,
) -> None:
    """Précision/rappel par tag et seuil maximisant F1 (hors ligne, sans inférence)."""
    scores_file = labels.scores_path(csv_path)
    if not csv_path.is_file() or not scores_file.is_file():
        console.print(f"[red]CSV ou fichier de scores introuvable : {escape(str(csv_path))}[/red]")
        raise typer.Exit(2)
    rows = labels.read_labels(csv_path)
    scores = json.loads(scores_file.read_text(encoding="utf-8"))
    thresholds = {t.name: t.threshold for t in load_taxonomy(tags).tags}
    n = sum(r[3] for r in rows)
    if not n:
        console.print("[red]Aucune ligne étiquetée (colonne true_tags vide).[/red]")
        raise typer.Exit(2)
    stats = labels.evaluate(rows, scores, thresholds)
    table = Table(
        "tag", "TP", "FP", "FN", "précision", "rappel", "seuil actuel", "seuil F1 max", "F1"
    )

    def pct(x: float | None) -> str:
        return "" if x is None else f"{x:.2f}"

    for s in stats:
        table.add_row(
            escape(s.tag),
            str(s.tp),
            str(s.fp),
            str(s.fn),
            pct(s.precision),
            pct(s.recall),
            f"{s.threshold:.2f}",
            pct(s.best_threshold),
            pct(s.best_f1),
        )
    console.print(f"{n} fichiers étiquetés sur {len(rows)}.")
    console.print(table)


@app.command("duplicates")
def duplicates(
    directory: Annotated[Path, typer.Argument(help="Dossier d'images et vidéos", metavar="DIR")],
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
    max_hamming: Annotated[
        int | None, typer.Option("--max-hamming", help="Distance dHash max (défaut : settings)")
    ] = None,
    window: Annotated[
        int | None, typer.Option("--window", help="Minutes entre prises (défaut : settings)")
    ] = None,
) -> None:
    """Cherche les doublons d'un dossier local : garde le meilleur, propose les autres."""
    if not directory.is_dir():
        console.print(f"[red]Dossier introuvable : {escape(str(directory))}[/red]")
        raise typer.Exit(2)
    paths = labels.scan_media(directory)
    if not paths:
        console.print("[red]Aucune image ou vidéo trouvée.[/red]")
        raise typer.Exit(2)
    settings = load_settings(config)
    state = State(":memory:")
    errors = 0
    for i, path in enumerate(paths, 1):
        try:
            if _is_video(path):
                duration = probe_duration(path)
                middle = extract_frames(path, 1, duration=duration)[0]
                sig = dedupe.video_signature(path, middle, probe_pixels(path))
                kind = "video"
            else:
                duration, kind = None, "image"
                sig = dedupe.image_signature(path)
        except Exception as e:
            errors += 1
            console.print(f"[red]{escape(path.name)} : {escape(str(e))}[/red]")
            continue
        state.upsert_file("local", i, str(path), None, None, None)
        state.upsert_hash(
            "local", i, kind, sig.dhash, sig.sharpness, sig.pixels, duration, sig.taken_at
        )
    names = {i: p for i, p in enumerate(paths, 1)}
    groups = dedupe.find_duplicate_groups(
        state.get_hashes(),
        max_hamming if max_hamming is not None else settings.quality.dup_max_hamming,
        window if window is not None else settings.quality.dup_window_minutes,
    )
    for n, g in enumerate(groups, 1):
        keeper = names[g.keeper.fileid]
        console.print(f"[bold]Groupe {n}[/bold]  gardé : [green]{escape(str(keeper))}[/green]")
        for r in g.flagged:
            console.print(
                f"  « {escape(settings.delete_tag_name)} » ({dedupe.REASON}) : "
                f"{escape(str(names[r.fileid]))}  "
                f"distance {dedupe.hamming(g.keeper.dhash, r.dhash)}",
                highlight=False,
            )
    flagged = sum(len(g.flagged) for g in groups)
    console.print(f"{len(paths)} fichiers, {len(groups)} groupes, {flagged} à supprimer proposés.")
    if errors:
        raise typer.Exit(1)


def _open_state(settings) -> State:
    return State(settings.state_db)


def _users(settings, user: str | None) -> list[str]:
    if user is None:
        return list(settings.nextcloud.users)
    if user not in settings.nextcloud.users:
        console.print(f"[red]Compte inconnu : {escape(user)}[/red]")
        raise typer.Exit(2)
    return [user]


def _credentials(users: list[str]) -> dict:
    creds = {u: get_app_password(u) for u in users}
    missing = [u for u, p in creds.items() if p is None]
    if missing:
        console.print(f"[red]Mot de passe d'application absent : {', '.join(missing)}[/red]")
        raise typer.Exit(2)
    return creds


def _waiting(reason: str, delay: float) -> None:
    why = "fenêtre sans écriture" if reason == "blackout" else f"serveur indisponible ({reason})"
    console.print(f"[yellow]Pause de {delay:.0f} s : {why}[/yellow]", highlight=False)


def _make_client(settings, creds: dict, read_only: bool = False):
    def make(user: str) -> NcClient:
        return NcClient(
            settings.nextcloud.base_url,
            user,
            creds[user],
            blackout=settings.nextcloud.blackout,
            concurrency=settings.nextcloud.concurrency,
            read_only=read_only,
            on_wait=_waiting,
        )

    return make


@app.command()
def inventory(
    user: Annotated[str | None, typer.Option("--user", help="Un seul compte")] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
) -> None:
    """Liste les images et vidéos de chaque compte (lecture seule) et met à jour l'état."""
    settings = load_settings(config)
    users = _users(settings, user)
    creds = _credentials(users)
    state = _open_state(settings)
    make = _make_client(settings, creds, read_only=True)

    async def one(name: str) -> tuple[Counter, float]:
        client = make(name)
        t0 = time.perf_counter()
        try:
            files = await webdav.inventory(client, name, settings.nextcloud.search_limit)
        finally:
            await client.aclose()
        return pipeline.sync_inventory(state, name, files), time.perf_counter() - t0

    table = Table("compte", "images", "vidéos", "nouveaux", "modifiés", "inchangés", "durée (s)")
    try:
        for name in users:
            counts, secs = asyncio.run(one(name))
            table.add_row(
                name,
                str(counts["images"]),
                str(counts["videos"]),
                str(counts["new"]),
                str(counts["changed"]),
                str(counts["unchanged"]),
                f"{secs:.1f}",
            )
    except NextcloudError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e
    console.print(table)


def parse_until(value: str, now: datetime) -> datetime:
    """« HH:MM » -> prochaine occurrence (demain si l'heure est déjà passée : passe minuit)."""
    try:
        hour, minute = (int(p) for p in value.split(":"))
        deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError as e:
        raise typer.BadParameter(f"« {value} » : attendu HH:MM") from e
    return deadline if deadline > now else deadline + timedelta(days=1)


def _plain(path: str, width: int = 80) -> str:
    return path if len(path) <= width else "…" + path[-(width - 1) :]


def _print_report(report: pipeline.RunReport, state: State, settings) -> None:
    c = report.counts
    console.print(
        f"Run {report.run_id} ({report.mode}) : {c['done']} traités, {c['skipped']} ignorés, "
        f"{c['error']} en erreur, {report.duplicates} doublons au total.",
        highlight=False,
    )
    if report.to_add:
        table = Table("tag", "fichiers", title="Tags à poser (fichiers traités)")
        for tag, n in report.to_add.most_common():
            table.add_row(escape(tag), str(n))
        console.print(table)
    if report.delete:
        table = Table("compte")
        table.add_column("fichier", overflow="fold")
        table.add_column("raisons")
        table.add_column("gardé (doublon)", overflow="fold")
        for f in report.delete:
            keeper = state.get_file(f.user, f.keeper) if f.keeper is not None else None
            table.add_row(
                f.user,
                escape(_plain(f.path)),
                ", ".join(f.reason_list),
                escape(_plain(keeper.path)) if keeper else "",
            )
        console.print(table)
        console.print(
            f"{len(report.delete)} fichiers proposés pour « {escape(settings.delete_tag_name)} » "
            "(l'outil ne supprime jamais rien).",
            highlight=False,
        )
    for f, status, detail in report.problems[:10]:
        style = "red" if status == "error" else "yellow"
        console.print(
            f"[{style}]{status} {f.user} {escape(_plain(f.path, 60))} : {escape(detail or '')}"
            f"[/{style}]",
            highlight=False,
        )
    if len(report.problems) > 10:
        console.print(f"... et {len(report.problems) - 10} autres.")


@app.command()
def run(
    user: Annotated[str | None, typer.Option("--user", help="Un seul compte")] = None,
    path: Annotated[
        str | None, typer.Option("--path", help="Préfixe de chemin, ex. /Photos")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Nombre max de fichiers")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Écrit les tags dans Nextcloud (défaut : dry-run)")
    ] = False,
    reprocess: Annotated[
        bool, typer.Option("--reprocess", help="Retraite aussi les fichiers déjà faits")
    ] = False,
    until: Annotated[
        str | None,
        typer.Option("--until", help="HH:MM : ne prend plus de nouveau fichier à cette heure"),
    ] = None,
    no_delete_tag: Annotated[
        bool,
        typer.Option(
            "--no-delete-tag",
            help="Ne pose pas « à supprimer » (propositions gardées pour « report »)",
        ),
    ] = False,
    exclude_path: Annotated[
        list[str] | None,
        typer.Option("--exclude-path", help="Préfixe de chemin à exclure (répétable)"),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
    tags: Annotated[Path, typer.Option("--tags", help="Chemin de tags.yaml")] = DEFAULT_TAGS,
) -> None:
    """Récupère (SFTP), analyse (d1) et propose les tags ; --apply les pose dans Nextcloud."""
    settings = load_settings(config)
    users = _users(settings, user)
    stop_at = parse_until(until, _now()) if until else None
    creds = _credentials(users) if apply else {}
    state = _open_state(settings)
    taxonomy = load_taxonomy(tags)
    done = 0

    def tick(f, status, detail) -> None:
        nonlocal done
        done += 1
        progress.update(f"{done} fichiers traités (dernier : {escape(Path(f.path).name)})")

    def apply_tick(f, status) -> None:
        nonlocal done
        done += 1
        progress.update(f"{done} fichiers appliqués")

    def apply_error(f, message) -> None:
        console.print(f"[red]{f.user} {escape(_plain(f.path, 60))} : {escape(message)}[/red]")

    def plan(report: pipeline.RunReport, todo: list) -> None:
        nonlocal done
        done = 0
        wanted = Counter(t for _, tags_ in todo for t in tags_)
        progress.stop()
        console.print(
            f"[bold]Application :[/bold] {len(todo)} fichiers, "
            f"{sum(wanted.values())} affectations voulues "
            f"(les tags déjà présents ne sont pas touchés ni journalisés).",
            highlight=False,
        )
        progress.start()

    async def go() -> pipeline.RunReport:
        d1 = _client(taxonomy, settings)
        try:
            async with SftpSource(settings.ssh) as source:
                return await pipeline.run(
                    settings,
                    state,
                    d1,
                    source,
                    users,
                    path_prefix=path,
                    limit=limit,
                    apply=apply,
                    reprocess=reprocess,
                    exclude_paths=exclude_path,
                    no_delete_tag=no_delete_tag,
                    stop_at=stop_at,
                    clock=_now,
                    make_client=_make_client(settings, creds),
                    on_item=tick,
                    on_apply_item=apply_tick,
                    on_apply_error=apply_error,
                    on_plan=plan,
                )
        finally:
            await d1.aclose()

    mode = "APPLY" if apply else "dry-run (rien n'est écrit dans Nextcloud)"
    console.print(f"Mode : {mode}", highlight=False)
    if stop_at:
        console.print(f"Arrêt demandé à {stop_at:%d/%m %H:%M}.", highlight=False)
    try:
        with console.status("Démarrage...") as progress:
            report = asyncio.run(go())
    except (pipeline.InferenceUnavailable, NextcloudError) as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e
    _print_report(report, state, settings)
    if report.stopped:
        console.print(
            "[yellow]Heure limite atteinte : arrêt propre, reprise au prochain lancement.[/yellow]",
            highlight=False,
        )
    console.print(f"Reste en attente : {report.remaining} fichiers.", highlight=False)
    if report.apply_counts is not None:
        a = report.apply_counts
        console.print(
            f"Application : {a['files']} fichiers, {a['added']} tags posés (journalisés), "
            f"{a['preexisting']} déjà présents (non journalisés), {a['removed']} retirés, "
            f"{a['tags_created']} tags créés, {a['errors']} erreurs.",
            highlight=False,
        )
    elif report.pending_apply:
        console.print(
            f"Dry-run : {report.pending_apply} fichiers attendent l'application "
            "(relancer avec --apply).",
            highlight=False,
        )
    if report.counts["error"] or (report.apply_counts and report.apply_counts["errors"]):
        raise typer.Exit(1)


@app.command("config-show")
def config_show(
    as_json: Annotated[bool, typer.Option("--json", help="Sortie JSON")] = False,
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
) -> None:
    """Affiche les réglages utiles aux scripts (llama-server, comptes, blackout) ; aucun secret."""
    settings = load_settings(config)
    llama = settings.llama
    data = {
        "llama": {
            "server_exe": str(llama.server_exe),
            "model": str(llama.model),
            "mmproj": str(llama.mmproj),
            "port": llama.port,
            "ub": llama.ub,
        },
        "users": settings.nextcloud.users,
        "blackout": settings.nextcloud.blackout,
        "delete_tag_name": settings.delete_tag_name,
    }
    if as_json:
        print(json.dumps(data, ensure_ascii=True))
        return
    for key, value in data.items():
        console.print(f"{key} : {escape(str(value))}", highlight=False)


def _ts(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M") if value else ""


@app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Compteurs par compte en JSON")] = False,
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
) -> None:
    """Fichiers par compte et statut, affectations journalisées, derniers runs."""
    settings = load_settings(config)
    state = _open_state(settings)
    if as_json:
        counts = {name: state.counts(name) for name in settings.nextcloud.users}
        print(json.dumps(counts, ensure_ascii=True))
        return
    table = Table("compte", *STATUSES, "tags journalisés")
    for name in settings.nextcloud.users:
        counts = state.counts(name)
        active = len(state.all_assignments(user=name))
        table.add_row(name, *(str(counts[s]) for s in STATUSES), str(active))
    console.print(table)
    runs = Table("run", "mode", "début", "fin", "résultat")
    for r in state.recent_runs():
        runs.add_row(
            str(r.id), r.mode, _ts(r.started_at), _ts(r.finished_at), escape(json.dumps(r.stats))
        )
    console.print(runs)


def _report_rows(state: State, settings, run_id: int | None, only_delete: bool) -> list[dict]:
    rows = []
    for name in settings.nextcloud.users:
        for f in state.list_files(name, statuses=("done",), run_id=run_id):
            if only_delete and not f.reasons:
                continue
            wanted = pipeline.wanted_tags(
                state, f, settings.delete_tag_name, settings.max_tags_per_file
            )
            keeper = state.get_file(name, f.keeper) if f.keeper is not None else None
            rows.append(
                {
                    "compte": name,
                    "fileid": f.fileid,
                    "chemin": f.path,
                    "tags": ", ".join(wanted),
                    "à supprimer": "oui" if f.reasons else "non",
                    "raisons": ", ".join(f.reason_list),
                    "gardé": keeper.path if keeper else "",
                    "appliqué": "oui" if f.applied == pipeline.wanted_key(wanted) else "non",
                }
            )
    return rows


@app.command()
def report(
    run: Annotated[int | None, typer.Option("--run", help="Fichiers traités par ce run")] = None,
    delete_candidates: Annotated[
        bool, typer.Option("--delete-candidates", help="Seulement les « à supprimer »")
    ] = False,
    csv_path: Annotated[Path | None, typer.Option("--csv", help="Écrit un CSV")] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
) -> None:
    """Liste les propositions de tags (dont « à supprimer » avec raison et fichier gardé)."""
    settings = load_settings(config)
    state = _open_state(settings)
    rows = _report_rows(state, settings, run, delete_candidates)
    if csv_path:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "compte",
                    "fileid",
                    "chemin",
                    "tags",
                    "à supprimer",
                    "raisons",
                    "gardé",
                    "appliqué",
                ],
                delimiter=";",
            )
            writer.writeheader()
            writer.writerows(rows)
        console.print(f"{len(rows)} lignes écrites dans {escape(str(csv_path))}")
        return
    table = Table("compte")
    table.add_column("chemin", overflow="fold")
    table.add_column("tags")
    table.add_column("raisons")
    table.add_column("gardé", overflow="fold")
    for r in rows[:200]:
        table.add_row(
            r["compte"],
            escape(r["chemin"]),
            escape(r["tags"]),
            r["raisons"],
            escape(r["gardé"]),
        )
    console.print(table)
    suffix = " (200 affichées, utiliser --csv pour tout)" if len(rows) > 200 else ""
    console.print(f"{len(rows)} fichiers{suffix}.", highlight=False)


@app.command()
def rollback(
    run: Annotated[int | None, typer.Option("--run", help="Run d'application à annuler")] = None,
    user: Annotated[str | None, typer.Option("--user", help="Un seul compte")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Confirme le retrait")] = False,
    config: Annotated[Path | None, typer.Option("--config", help="Chemin de settings.toml")] = None,
) -> None:
    """Retire UNIQUEMENT les tags journalisés par l'outil (jamais ceux déjà présents)."""
    settings = load_settings(config)
    users = _users(settings, user)
    state = _open_state(settings)
    todo = [a for a in state.all_assignments(run, user) if a.user in users]
    if not todo:
        console.print("Rien à retirer : aucune affectation journalisée active.")
        return
    table = Table("compte", "run", "tag", "fichiers")
    groups = Counter((a.user, a.run_id, a.tag) for a in todo)
    for (name, run_id, tag), n in sorted(groups.items()):
        table.add_row(name, str(run_id), escape(tag), str(n))
    console.print(table)
    console.print(f"{len(todo)} affectations journalisées seraient retirées.", highlight=False)
    if not yes:
        console.print("[yellow]Rien n'est retiré : relancer avec --yes pour confirmer.[/yellow]")
        raise typer.Exit(1)
    creds = _credentials(sorted({a.user for a in todo}))
    run_id = state.start_run("rollback")
    counts: Counter = Counter()
    try:
        counts = asyncio.run(
            pipeline.rollback_assignments(state, todo, _make_client(settings, creds))
        )
    except NextcloudError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e
    finally:
        state.finish_run(run_id, dict(counts))
    console.print(
        f"Retirées : {counts['removed']}, déjà absentes : {counts['already_gone']}, "
        f"erreurs : {counts['errors']}.",
        highlight=False,
    )
    if counts["errors"]:
        raise typer.Exit(1)
