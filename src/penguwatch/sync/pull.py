"""Pull half of the sync engine: applying what the library driver yields, and the incoming review (staged changes).

A pull applies the titles in write transactions of PULL_BATCH_SIZE titles. If one fails, its whole batch is rolled
back and earlier batches stay committed; the pull aborts and the cursor is not saved, so the next pull simply redoes
it.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from penguwatch.drivers.base import LOCAL_ID, LibraryEntry, PullBatch, SyncChange
from penguwatch.models import MediaType, Show
from penguwatch.sync import ops as sync_ops
from penguwatch.sync.enrich import _iso, _utcnow
from penguwatch.sync.ops import STATUS_LABELS, describe_episodes
from penguwatch.sync.titles import entry_to_show

logger = logging.getLogger("penguwatch.sync")

# How many titles one write transaction applies during a pull (one commit per batch instead of one per
# statement). A failure rolls back its whole batch: earlier batches stay committed, the pull aborts, and since
# the cursor is only saved by a pull that finished, the next pull redoes the rest.
PULL_BATCH_SIZE = 50


@dataclass
class _PullCtx:
    """State shared by the steps of one pull."""

    pending: set  # titles with an unsent change: the pull leaves them alone
    stage: bool
    stats: Dict[str, Any]
    staged: List[Dict[str, Any]] = field(default_factory=list)
    # Titles with rows held for this full pull ("add if missing", SyncOp.if_missing): `held_only` have no other
    # unsent change, so they do not lock the title; `held` are all of them and are never removed as "missing"
    held: set = field(default_factory=set)
    held_only: set = field(default_factory=set)
    # what the listings of this pull showed (every media type): the held rows are resolved against it once the whole
    # pull succeeded (_finish_pull), never part by part
    account_keys: set = field(default_factory=set)
    listed: bool = False  # a full listing ran

    @property
    def staged_sink(self) -> Optional[List[Dict[str, Any]]]:
        return self.staged if self.stage else None


class PullMixin:
    # ------------------------------------------------------------------ pull

    def _key(self, change: SyncChange) -> Any:
        return change.ids.get(self.library.key_provider)

    async def _apply_change(self, change: SyncChange, pending: set) -> Optional[int]:
        """Applies one library entry in a transaction of its own (a batch of one)."""
        key = self._key(change)
        if not key:
            return None
        async with self.repository.write() as db:
            existing = (await self.repository.shows_by_external_ids_tx(db, self.library.key_provider, [key])).get(key)
            return await self._apply_entry_tx(db, change.entry, MediaType(change.media_type), pending, existing)

    async def _apply_entry_tx(self, db, entry: Optional[LibraryEntry], media_type: MediaType, pending: set,
                              existing: Optional[Show]) -> Optional[int]:
        """Applies one entry inside the caller's write transaction. `existing` is the local title with the entry's
        key as it stood when the transaction began (None if new): the entries of one batch have distinct keys, so
        nothing the batch wrote can make it stale."""
        if entry is None:
            return None
        remote = entry_to_show(entry)
        key = remote.ids.get(self.library.key_provider)
        if not key:
            return None
        repo = self.repository
        locked = key in pending  # unsent local edits win until delivered

        if existing is None:
            if locked:
                return None  # a change of this title (its removal, say) is waiting to be sent: do not bring it back
            if media_type == MediaType.ANIME and not remote.network:
                remote.service_id = (await repo.match_service_by_network(None, title=remote.title)
                                     or await repo.default_anime_service())
            if not remote.service_id:
                remote.service_id = await repo.match_service_by_network(remote.network, title=remote.title)
            show_id = await repo._upsert_show_tx(db, remote)
        else:
            show_id = existing.id
            fields: Dict[str, Any] = {
                "title": remote.title,
                "media_type": media_type.value,
                "is_ended": 1 if remote.is_ended else 0,
            }
            for col in ("year", "poster", "banner", "overview", "network", "simkl_rating", "imdb_rating", "imdb_votes"):
                val = getattr(remote, col)
                if val not in (None, ""):
                    fields[col] = val
            fields["ids"] = remote.ids
            if remote.genres:
                merged = list(dict.fromkeys(list(existing.genres) + list(remote.genres)))
                fields["genres_json"] = json.dumps(merged)
            if not locked:
                fields["status"] = remote.status.value
                if entry.has_rating:
                    fields["user_rating"] = remote.user_rating
                fields["watched_episodes"] = remote.watched_episodes
                if remote.total_episodes:
                    fields["total_episodes"] = remote.total_episodes
                if remote.aired_episodes:
                    fields["aired_episodes"] = remote.aired_episodes
                fields["last_watched"] = remote.last_watched
                fields["next_to_watch"] = remote.next_to_watch
                if remote.last_watched_at:
                    fields["last_watched_at"] = remote.last_watched_at
            await repo._update_show_fields_tx(db, show_id, fields)

        if not locked and media_type != MediaType.MOVIE:
            watched = entry.watched
            # A bulk-completed show may come back without per-episode rows; don't wipe local flags then.
            if watched is not None and (watched or remote.watched_episodes == 0):
                await repo._replace_watched_episodes_tx(db, show_id, watched)
                await repo._recompute_progress_tx(db, show_id)
        return show_id

    async def _apply_batch(self, ctx: "_PullCtx", batch: List[Tuple[Any, SyncChange]], media_type: MediaType) -> None:
        """Applies (key, change) pairs, in order, in ONE write transaction. The keys must be distinct."""
        if not batch:
            return
        async with self.repository.write() as db:
            existing = await self.repository.shows_by_external_ids_tx(db, self.library.key_provider, [k for k, _ in batch])
            applied = [
                await self._apply_entry_tx(db, c.entry, media_type, ctx.pending, existing.get(k)) for k, c in batch
            ]
        for show_id in applied:  # counted once the batch is committed
            if show_id:
                ctx.stats["updated"] += 1
                ctx.stats["touched_ids"].append(show_id)

    async def _remove_missing(self, media_type: MediaType, remote_keys: frozenset, pending: set,
                              staged: Optional[List[Dict[str, Any]]] = None) -> int:
        provider = self.library.key_provider
        local = await self.repository.get_external_ids(provider, media_type)
        # A title that was added while the library had no account was never in the listing: it is not "removed"
        born_local = set((await self.repository.get_external_ids(LOCAL_ID)).values())
        missing = [key for key in local
                   if key not in remote_keys and key not in pending and local[key] not in born_local]
        if not remote_keys and len(local) > 5:
            logger.warning(f"{self.library.label} returned no {media_type.value} ids; skipping removal of "
                           f"{len(local)} local titles as a safety measure.")
            return 0
        for key in missing:
            if staged is not None:
                show = await self.repository.get_show_by_id(local[key])
                staged.append({
                    "key": f"{media_type.value}:{key}", "kind": "removed", "media": media_type.value,
                    "ids": {provider: key}, "show_id": local[key], "title": show.title if show else str(key),
                    "poster": show.poster if show else None,
                    "changes": [{"field": "library", "local": "in library",
                                 "remote": f"removed on {self.library.label}"}],
                })
            else:
                await self.repository.delete_show(local[key])
        if missing and staged is None:
            logger.info(f"Removed {len(missing)} {media_type.value} titles that were removed on {self.library.label}.")
        return len(missing)

    async def _diff_change(self, change: SyncChange, pending: set) -> Optional[Dict[str, Any]]:
        """What applying this entry would change locally (None if nothing the user would care about)."""
        entry = change.entry
        if entry is None:
            return None
        media_type = MediaType(change.media_type)
        remote = entry_to_show(entry)
        provider = self.library.key_provider
        key = remote.ids.get(provider)
        if not key or key in pending:
            return None
        base = {
            "key": f"{media_type.value}:{key}", "media": media_type.value, "ids": {provider: key},
            "title": remote.title, "poster": remote.poster, "item": change.native,
            **({"en_title": remote.en_title} if remote.en_title else {}),  # the page names titles by it
        }
        existing = await self.repository.get_show_by_external_id(provider, key)
        if existing is None:
            return {**base, "kind": "new", "show_id": None,
                    "changes": [{"field": "status", "local": None, "remote": remote.status.value}]}
        changes: List[Dict[str, Any]] = []
        if existing.status != remote.status:
            changes.append({"field": "status", "local": existing.status.value, "remote": remote.status.value})
        if entry.has_rating:
            lr = int(round(existing.user_rating)) if existing.user_rating else None
            rr = int(round(remote.user_rating)) if remote.user_rating else None
            if lr != rr:
                changes.append({"field": "rating", "local": lr, "remote": rr})
        if media_type != MediaType.MOVIE:
            watched = entry.watched
            local_eps = [e for e in await self.repository.get_episodes(existing.id) if e.is_watched and e.season > 0]
            local_set = {(e.season, e.number) for e in local_eps}
            if watched is not None and (watched or remote.watched_episodes == 0) and (local_set or existing.watched_episodes == 0):
                remote_set = {(s_, n) for s_, n, _ in watched}
                added, removed = sorted(remote_set - local_set), sorted(local_set - remote_set)
                if added or removed:
                    changes.append({
                        "field": "episodes", "local": len(local_set), "remote": len(remote_set),
                        "added": describe_episodes(added) if added else None,
                        "removed": describe_episodes(removed) if removed else None,
                    })
            elif remote.watched_episodes != existing.watched_episodes:
                changes.append({"field": "watched", "local": existing.watched_episodes, "remote": remote.watched_episodes})
        if not changes:
            return None
        return {**base, "kind": "update", "show_id": existing.id, "changes": changes}

    async def _stage_or_apply(self, ctx: "_PullCtx", change: SyncChange) -> Optional[int]:
        """Review mode: record the difference for approval; otherwise apply the library's version now."""
        if ctx.stage:
            entry = await self._diff_change(change, ctx.pending)
            if entry:
                ctx.staged.append(entry)
                return None
        show_id = await self._apply_change(change, ctx.pending)
        if show_id:
            ctx.stats["updated"] += 1
            ctx.stats["touched_ids"].append(show_id)
        return show_id

    async def _stage_or_apply_many(self, ctx: "_PullCtx", changes: List[SyncChange], media_type: MediaType) -> None:
        """_stage_or_apply for a list: entries that are applied are collected and written PULL_BATCH_SIZE at a time.

        An entry whose key is already in the open batch flushes it first, so every entry sees the state the previous
        ones left (in review mode its diff is computed on committed rows)."""
        batch: List[Tuple[Any, SyncChange]] = []
        in_batch: set = set()

        async def flush() -> None:
            await self._apply_batch(ctx, batch, media_type)
            batch.clear()
            in_batch.clear()

        for change in changes:
            key = self._key(change)
            if not key:
                continue  # nothing to stage or apply
            if key in in_batch:
                await flush()
            if ctx.stage:
                entry = await self._diff_change(change, ctx.pending)
                if entry:
                    ctx.staged.append(entry)
                    continue
            batch.append((key, change))
            in_batch.add(key)
            if len(batch) >= max(1, PULL_BATCH_SIZE):
                await flush()
        await flush()

    async def _apply_ratings(self, ctx: "_PullCtx", changes: List[SyncChange], media_type: MediaType) -> None:
        provider = self.library.key_provider
        for change in changes:
            key = self._key(change)
            if not key or key in ctx.pending:
                continue
            local = await self.repository.get_show_by_external_id(provider, key)
            if not local:
                await self._stage_or_apply(ctx, change)
                continue
            lr = int(round(local.user_rating)) if local.user_rating else None
            rr = change.rating
            if lr == rr:
                continue
            if ctx.stage:
                ctx.staged.append({
                    "key": f"{media_type.value}:{key}", "kind": "update", "media": media_type.value,
                    "ids": {provider: key}, "show_id": local.id, "title": local.title, "poster": local.poster,
                    "rating_only": True, "item": change.native,
                    "changes": [{"field": "rating", "local": lr, "remote": rr}],
                })
            else:
                await self.repository.update_show_fields(local.id, {"user_rating": rr})

    async def _apply_pull_batch(self, ctx: "_PullCtx", part: PullBatch) -> None:
        media_type = MediaType(part.media_type)
        entries = [c for c in part.changes if c.kind == "entry"]
        ratings = [c for c in part.changes if c.kind == "rating"]
        if entries:
            await self._stage_or_apply_many(ctx, entries, media_type)
        if ratings:
            await self._apply_ratings(ctx, ratings, media_type)
        if part.listing:
            ctx.stats["removed"] += await self._remove_missing(media_type, part.remote_keys, ctx.pending | ctx.held,
                                                               ctx.staged_sink)
            ctx.account_keys |= set(part.remote_keys)
            ctx.listed = True

    async def _resolve_held(self, ctx: "_PullCtx") -> None:
        """Adds made before the login ("add if missing", SyncOp.if_missing) are decided here, once every listing of the
        pull has run: the account has the title -> its state came in above, the add is dropped and later edits wait
        for the person (unapproved); it lacks it -> the rows go out as usual. A pull that failed earlier never gets
        here and leaves every held row as it was."""
        if not ctx.held or not ctx.listed or ctx.stats.get("mode") != "full":
            return
        out = await self.repository.resolve_flagged_outbox(self.driver, ctx.account_keys)
        if out["released"]:
            self.kick.set()

    async def _finish_pull(self, ctx: "_PullCtx", cursor: Any) -> Dict[str, Any]:
        await self._resolve_held(ctx)
        stats = ctx.stats
        if ctx.staged:
            # Keep the old cursor so these changes are re-fetched until the user decides
            merged: Dict[str, Dict[str, Any]] = {}
            for e in ctx.staged:
                prev = merged.get(e["key"])
                if prev and prev.get("rating_only") is None and e.get("rating_only"):
                    continue  # full entry already covers the rating
                merged[e["key"]] = e
            await self.repository.set_state("incoming_staged", json.dumps({
                "cursor": cursor, "at": _iso(_utcnow()), "entries": list(merged.values()),
            }))
            stats["staged"] = len(merged)
        else:
            await self.repository.delete_state("incoming_staged")
            await self.repository.set_state(self.state_key("cursor"), json.dumps(cursor))
        return stats

    async def _unchanged_since_staged(self, cursor: Any) -> Optional[int]:
        """Entries still awaiting review if the library has not moved since they were staged, else None.

        The old cursor is kept while changes await review, so without this check every cycle would fetch the very
        same delta again."""
        raw = await self.repository.get_state("incoming_staged")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if data.get("cursor") != cursor:
            return None
        return len(data.get("entries") or [])

    async def _pull(self, force_full: bool) -> Dict[str, Any]:
        provider = self.library.key_provider
        held, held_only = await self.repository.flagged_remote_ids(self.driver)
        pending = await self.repository.pending_remote_ids(self.driver) - held_only
        force_full = force_full or bool(held)  # adds made before the login wait for a full pull's listing
        # In review mode, changes coming from the library are staged for approval instead of applied.
        # (The very first import into an empty library is applied directly: there is nothing to protect.)
        stage = await self.review_mode() and bool(await self.repository.get_external_ids(provider))
        saved_raw = await self.get_state("cursor")
        saved = json.loads(saved_raw) if saved_raw else None
        plan = await self.library.plan_pull(saved, force_full)
        if not force_full:
            awaiting = await self._unchanged_since_staged(plan.cursor)
            if awaiting is not None:
                return {"mode": "delta", "updated": 0, "removed": 0, "touched_ids": [], "staged": awaiting,
                        "skipped": "unchanged since staged"}
        ctx = _PullCtx(pending=pending, stage=stage, held=held, held_only=set(held_only),
                       stats={"mode": "delta", "updated": 0, "removed": 0, "touched_ids": []}, staged=[])
        if plan.mode == "full":
            ctx.stats["mode"] = "full"
        async for part in self.library.pull(plan, saved):
            await self._apply_pull_batch(ctx, part)
        return await self._finish_pull(ctx, plan.cursor)

    # ------------------------------------------------------------------ review (incoming)

    async def get_incoming(self) -> Dict[str, Any]:
        raw = await self.repository.get_state("incoming_staged")
        if not raw:
            return {"entries": [], "at": None}
        data = json.loads(raw)
        return {"at": data.get("at"), "entries": [{k: v for k, v in e.items() if k != "item"} for e in data["entries"]]}

    def _staged_change(self, e: Dict[str, Any]) -> SyncChange:
        entry = self.library.decode_entry(e["media"], e.get("item"))
        return SyncChange(kind="entry", media_type=e["media"], ids=dict(e.get("ids") or {}), entry=entry,
                          native=e.get("item"))

    async def resolve_incoming(self, keys: Optional[List[str]], accept: bool) -> Dict[str, Any]:
        """accept=True applies the library's version locally; accept=False keeps ours and sends it there."""
        raw = await self.repository.get_state("incoming_staged")
        if not raw:
            return {"resolved": 0, "remaining": 0}
        data = json.loads(raw)
        entries = data["entries"]
        chosen = [e for e in entries if keys is None or e["key"] in set(keys)]
        new_ids: List[int] = []
        provider = self.library.key_provider
        for e in chosen:
            mt = MediaType(e["media"])
            local = await self.repository.get_show_by_external_id(provider, (e.get("ids") or {}).get(provider))
            if accept:
                if e["kind"] == "removed":
                    if local:
                        await self.repository.delete_show(local.id)
                elif e.get("rating_only"):
                    if local:
                        await self.repository.update_show_fields(local.id, {"user_rating": e["changes"][0]["remote"]})
                else:
                    show_id = await self._apply_change(self._staged_change(e), set())
                    if show_id and e["kind"] == "new":
                        new_ids.append(show_id)
            else:
                await self._keep_local(e, mt, local)
        remaining = [e for e in entries if e not in chosen]
        if remaining:
            data["entries"] = remaining
            await self.repository.set_state("incoming_staged", json.dumps(data))
        else:
            await self.repository.delete_state("incoming_staged")
            await self.repository.set_state(self.state_key("cursor"), json.dumps(data["cursor"]))
        if new_ids:
            await self.enrich_titles(new_ids, pace=False)  # a person is waiting
        return {"resolved": len(chosen), "remaining": len(remaining)}

    async def _keep_local(self, e: Dict[str, Any], mt: MediaType, local: Optional[Show]):
        """'Keep mine': queue (pre-approved) pushes that make the library match PenguWatch again."""
        if e["kind"] == "new":
            change = self._staged_change(e)
            if change.entry:
                await self.remove_from_library(entry_to_show(change.entry), force_approved=True)
            return
        if not local:
            return
        if e["kind"] == "removed":
            await self.queue_list_move(
                local, local.status, summary=f"Re-add to {self.library.label} as {STATUS_LABELS[local.status.value]}",
                force_approved=True)
            return
        for ch in e["changes"]:
            if ch["field"] == "status":
                await self.queue_list_move(local, local.status, force_approved=True)
            elif ch["field"] == "rating":
                r = int(round(local.user_rating)) if local.user_rating else None
                await self._queue(sync_ops.rating_op(local, r), local, force_approved=True)
            elif ch["field"] == "episodes":
                entry = self._staged_change(e).entry
                remote_set = {(s_, n) for s_, n, _ in (entry.watched if entry and entry.watched else [])}
                local_set = {(x.season, x.number) for x in await self.repository.get_episodes(local.id) if x.is_watched and x.season > 0}
                extra_remote, missing_remote = remote_set - local_set, local_set - remote_set
                if extra_remote:
                    await self._queue(sync_ops.episodes_op(local, extra_remote, False, None), local,
                                      force_approved=True)
                if missing_remote:
                    await self._queue(sync_ops.episodes_op(local, missing_remote, True, _iso(_utcnow())), local,
                                      force_approved=True)
