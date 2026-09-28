"""Durable, explicit lifecycle for ordinary SINGLE_MODE report collections."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

from .panel_jobs import PanelJobError, PanelJobRegistry, TERMINAL
from .performance_v2_collection import build_single_mode_collection_inbox


_COLLECTION_KIND = "strategies.tester.collection"
_ACTIVE_STATES = frozenset({"QUEUED", "RUNNING", "CANCELLING"})
_COLLECTION_STATES = frozenset({"OPEN", "VERIFIED", "IMPORTED", "CLEARED"})


class PanelReportCollection:
    """Keep collection membership in the panel journal, never in directories."""

    def __init__(
        self,
        registry: PanelJobRegistry,
        *,
        inbox_root: Path,
        report_root: Path,
        trusted_strategy_root: Path,
    ) -> None:
        self.registry = registry
        self.inbox_root = Path(inbox_root)
        self.report_root = Path(report_root)
        self.trusted_strategy_root = Path(trusted_strategy_root)

    @staticmethod
    def _names(expected_names: Sequence[str]) -> list[str]:
        if isinstance(expected_names, (str, bytes)):
            raise PanelJobError("COLLECTION_NAMES_INVALID")
        names = list(expected_names)
        if not names or any(not isinstance(name, str) or not name.strip() for name in names):
            raise PanelJobError("COLLECTION_NAMES_INVALID")
        folded = [name.casefold() for name in names]
        if len(set(folded)) != len(folded):
            raise PanelJobError("COLLECTION_NAMES_INVALID")
        return names

    @staticmethod
    def _is_ordinary(job: dict[str, object]) -> bool:
        kind = job.get("kind")
        runtime = job.get("runtime")
        return (
            kind in {"strategies.tester.start", "strategies.tester.retry"}
            and job.get("retest") is not True
            and not (isinstance(runtime, dict) and runtime.get("retest") is True)
        )

    def _records(self) -> list[dict[str, object]]:
        return [job for job in self.registry.list() if job.get("kind") == _COLLECTION_KIND]

    def _runtime(self, record: dict[str, object]) -> dict[str, object]:
        runtime = record.get("runtime")
        if not isinstance(runtime, dict):
            job_id = record.get("job_id")
            if isinstance(job_id, str):
                runtime = self.registry.runtime(job_id)
        if not isinstance(runtime, dict):
            raise PanelJobError("COLLECTION_INVALID")
        state = runtime.get("collection_state")
        members = runtime.get("members")
        if state not in _COLLECTION_STATES or not isinstance(members, list):
            raise PanelJobError("COLLECTION_INVALID")
        return runtime

    def _latest(self) -> dict[str, object] | None:
        records = self._records()
        return records[-1] if records else None

    def _active(self) -> dict[str, object] | None:
        record = self._latest()
        if record is None:
            return None
        try:
            return record if self._runtime(record).get("collection_state") in {"OPEN", "VERIFIED"} else None
        except PanelJobError:
            return None

    @staticmethod
    def _revision(runtime: Mapping[str, object]) -> int:
        value = runtime.get("collection_revision", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PanelJobError("COLLECTION_INVALID")
        return value

    def _persist_runtime(
        self,
        collection_id: str,
        runtime: dict[str, object],
        *,
        expected_revision: int | None = None,
    ) -> None:
        current = self.registry.runtime(collection_id)
        current_revision = self._revision(current)
        if expected_revision is not None and current_revision != expected_revision:
            raise PanelJobError("COLLECTION_CHANGED_DURING_VERIFY")
        runtime = deepcopy(runtime)
        runtime["collection_revision"] = current_revision + 1
        status: dict[str, object] = {"state": "COMMITTED", "phase": "COMMITTED"}
        if isinstance(runtime.get("verified_inbox_path"), str) and runtime.get("collection_state") == "VERIFIED":
            status["inbox_ready"] = True
        self.registry.sync(collection_id, status, runtime=runtime)

    def _member(self, runtime: dict[str, object] | list[dict[str, object]], job_id: str) -> dict[str, object] | None:
        members = runtime.get("members") if isinstance(runtime, dict) else runtime
        if not isinstance(members, list):
            return None
        for member in members:
            if isinstance(member, dict) and member.get("tester_job_id") == job_id:
                return member
        return None

    @staticmethod
    def _read_manifest_binding(collection_id: str, path: Path, expected_digest: str | None = None) -> str:
        try:
            data = path.joinpath("inbox_manifest.json").read_bytes()
            document = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise PanelJobError("COLLECTION_VERIFIED_INBOX_TAMPERED") from None
        digest = sha256(data).hexdigest()
        if expected_digest is not None and digest != expected_digest:
            raise PanelJobError("COLLECTION_VERIFIED_INBOX_TAMPERED")
        if not isinstance(document, dict) or document.get("collection_id") != collection_id:
            raise PanelJobError("COLLECTION_MANIFEST_ID_MISMATCH")
        return digest

    def assert_importable(self, collection_id: str) -> Path:
        record = self.registry.get(collection_id)
        if record.get("kind") != _COLLECTION_KIND:
            raise PanelJobError("COLLECTION_NOT_FOUND")
        runtime = self._runtime(record)
        path = runtime.get("verified_inbox_path")
        if runtime.get("collection_state") != "VERIFIED" or runtime.get("performance_v2_import_verified") is not True:
            raise PanelJobError("COLLECTION_IMPORT_NOT_AUTHORIZED")
        if not isinstance(path, str) or not path.strip():
            raise PanelJobError("COLLECTION_VERIFIED_INBOX_UNAVAILABLE")
        inbox = Path(path)
        self._read_manifest_binding(collection_id, inbox, runtime.get("verified_inbox_sha256"))
        return inbox

    def import_snapshot(self, collection_id: str) -> dict[str, str]:
        """Read the currently authorized generation without consuming its gate."""
        with self.registry.lock:
            record = self.registry.get(collection_id)
            if record.get("kind") != _COLLECTION_KIND:
                raise PanelJobError("COLLECTION_NOT_FOUND")
            runtime = self._runtime(record)
            if runtime.get("collection_state") != "VERIFIED" or runtime.get("performance_v2_import_verified") is not True:
                raise PanelJobError("COLLECTION_IMPORT_NOT_AUTHORIZED")
            if runtime.get("import_in_progress") is not None:
                raise PanelJobError("COLLECTION_IMPORT_IN_PROGRESS")
            path = runtime.get("verified_inbox_path")
            if not isinstance(path, str) or not path.strip():
                raise PanelJobError("COLLECTION_VERIFIED_INBOX_UNAVAILABLE")
            digest = self._read_manifest_binding(collection_id, Path(path), runtime.get("verified_inbox_sha256"))
            return {"inbox_path": path, "manifest_sha256": digest}

    def claim_import(self, collection_id: str, import_job_id: str, *, expected_digest: str | None = None) -> dict[str, str]:
        """Atomically consume the import gate for one exact verified generation."""
        if not isinstance(import_job_id, str) or not import_job_id.strip():
            raise PanelJobError("INVALID_REQUEST")
        with self.registry.lock:
            record = self.registry.get(collection_id)
            if record.get("kind") != _COLLECTION_KIND:
                raise PanelJobError("COLLECTION_NOT_FOUND")
            runtime = self._runtime(record)
            if runtime.get("collection_state") != "VERIFIED" or runtime.get("performance_v2_import_verified") is not True:
                raise PanelJobError("COLLECTION_IMPORT_NOT_AUTHORIZED")
            if runtime.get("import_in_progress") is not None:
                raise PanelJobError("COLLECTION_IMPORT_IN_PROGRESS")
            path = runtime.get("verified_inbox_path")
            if not isinstance(path, str) or not path.strip():
                raise PanelJobError("COLLECTION_VERIFIED_INBOX_UNAVAILABLE")
            digest = self._read_manifest_binding(collection_id, Path(path), runtime.get("verified_inbox_sha256"))
            if expected_digest is not None and digest != expected_digest:
                raise PanelJobError("COLLECTION_VERIFIED_INBOX_TAMPERED")
            revision = self._revision(runtime)
            runtime["import_in_progress"] = import_job_id
            runtime["import_generation_revision"] = revision
            runtime["performance_v2_import_verified"] = False
            self._persist_runtime(collection_id, runtime, expected_revision=revision)
            return {"inbox_path": path, "manifest_sha256": digest}

    def finish_import(self, collection_id: str, import_job_id: str, *, committed: bool) -> None:
        """Release a claim, or consume its generation after committed import."""
        with self.registry.lock:
            record = self.registry.get(collection_id)
            if record.get("kind") != _COLLECTION_KIND:
                raise PanelJobError("COLLECTION_NOT_FOUND")
            runtime = self._runtime(record)
            if runtime.get("import_in_progress") != import_job_id:
                raise PanelJobError("COLLECTION_IMPORT_CLAIM_MISMATCH")
            revision = self._revision(runtime)
            runtime.pop("import_in_progress", None)
            runtime.pop("import_generation_revision", None)
            if committed:
                runtime["collection_state"] = "IMPORTED"
                runtime["performance_v2_import_verified"] = False
            else:
                runtime["performance_v2_import_verified"] = True
            self._persist_runtime(collection_id, runtime, expected_revision=revision)

    def release_import_claim(self, collection_id: str, import_job_id: str) -> None:
        self.finish_import(collection_id, import_job_id, committed=False)

    def _new_record(self, members: list[dict[str, object]]) -> str:
        collection_id = f"collection-{uuid4().hex}"
        request = {"collection_id": collection_id, "member_count": len(members)}
        self.registry.submit(
            _COLLECTION_KIND,
            request,
            f"panel:collection:{collection_id}",
            (),
            job_id=collection_id,
        )
        self.registry.transition(collection_id, "RUNNING")
        self.registry.transition(collection_id, "COMMITTED")
        runtime = {
            "collection_state": "OPEN",
            "members": deepcopy(members),
            "collection_revision": 0,
            "verified_inbox_path": None,
            "verified_inbox_sha256": None,
            "performance_v2_import_verified": False,
        }
        self._persist_runtime(collection_id, runtime)
        return collection_id

    def register(
        self,
        tester_job_id: str,
        expected_names: Sequence[str],
        *,
        replaces_job_id: str | None = None,
    ) -> str:
        if not isinstance(tester_job_id, str) or not tester_job_id.strip():
            raise PanelJobError("INVALID_REQUEST")
        names = self._names(expected_names)
        tester = self.registry.get(tester_job_id)
        if not self._is_ordinary(tester):
            raise PanelJobError("COLLECTION_ORDINARY_ONLY")
        if tester.get("state") in TERMINAL and tester.get("state") not in {"COMMITTED", "FAILED", "CANCELLED"}:
            raise PanelJobError("COLLECTION_MEMBER_STATE_INVALID")
        with self.registry.lock:
            active = self._active()
            active_runtime = self._runtime(active) if active is not None else None
            if active_runtime is not None and active_runtime["collection_state"] == "OPEN":
                members = deepcopy(active_runtime["members"])
                collection_id = str(active["job_id"])
            else:
                if active_runtime is not None and active_runtime["collection_state"] == "VERIFIED" and active_runtime.get("import_in_progress") is not None:
                    raise PanelJobError("COLLECTION_IMPORT_IN_PROGRESS")
                members = deepcopy(active_runtime["members"]) if active_runtime is not None else []
                if active_runtime is not None and active_runtime["collection_state"] == "VERIFIED":
                    active_runtime["performance_v2_import_verified"] = False
                    self._persist_runtime(str(active["job_id"]), active_runtime)
                collection_id = self._new_record(members)
                active = self.registry.get(collection_id)
                active_runtime = self._runtime(active)
            if self._member(active_runtime, tester_job_id) is not None:
                raise PanelJobError("COLLECTION_MEMBER_ALREADY_REGISTERED")
            if replaces_job_id is not None:
                if not isinstance(replaces_job_id, str) or not replaces_job_id.strip():
                    raise PanelJobError("INVALID_REQUEST")
                old = self._member(members, replaces_job_id)
                if old is None or old.get("superseded") is True:
                    raise PanelJobError("COLLECTION_RETRY_SOURCE_NOT_REGISTERED")
                source = self.registry.get(replaces_job_id)
                if source.get("state") not in {"FAILED", "CANCELLED"}:
                    raise PanelJobError("COLLECTION_RETRY_SOURCE_NOT_FAILED")
                old["superseded"] = True
                old["superseded_by"] = tester_job_id
            member = {
                "tester_job_id": tester_job_id,
                "expected_strategy_names": names,
                "retry_source_job_id": replaces_job_id,
                "superseded": False,
                "registered_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            members.append(member)
            active_runtime["members"] = members
            self._persist_runtime(collection_id, active_runtime)
            tester_runtime = self.registry.runtime(tester_job_id)
            tester_runtime["report_collection_id"] = collection_id
            self.registry.sync(tester_job_id, {"state": tester["state"]}, runtime=tester_runtime)
            return collection_id

    @staticmethod
    def _manifest_report_count(inbox: Path) -> int:
        try:
            document = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
            entries = document.get("entries") if isinstance(document, dict) else None
            return len(entries) if isinstance(entries, list) else 0
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return 0

    def _member_view(self, member: dict[str, object]) -> dict[str, object]:
        job_id = member.get("tester_job_id")
        try:
            job = self.registry.get(str(job_id))
            runtime = self.registry.runtime(str(job_id))
        except PanelJobError:
            job, runtime = {"state": "FAILED"}, {}
        inbox = runtime.get("inbox_path")
        committed = (
            job.get("state") == "COMMITTED"
            and job.get("inbox_ready") is True
            and isinstance(inbox, str)
            and bool(inbox)
            and member.get("superseded") is not True
        )
        view = deepcopy(member)
        view.update({
            "state": job.get("state"),
            "inbox_path": inbox if isinstance(inbox, str) else None,
            "committed": committed,
            "report_count": self._manifest_report_count(Path(inbox)) if committed else 0,
        })
        return view

    def status(self) -> dict[str, object]:
        record = self._active() or self._latest()
        if record is None:
            return {
                "collection_id": None,
                "state": "EMPTY",
                "verification_state": "EMPTY",
                "total_registered_packs": 0,
                "committed_packs": 0,
                "active_packs": 0,
                "failed_cancelled_packs": 0,
                "exact_committed_report_count": 0,
                "importable_collection_job_id": None,
                "members": [],
            }
        runtime = self._runtime(record)
        members = [self._member_view(member) for member in runtime["members"] if isinstance(member, dict)]
        current = [member for member in members if member.get("superseded") is not True]
        active = [member for member in current if member.get("state") in _ACTIVE_STATES]
        failed = [member for member in current if member.get("state") in {"FAILED", "CANCELLED"}]
        committed = [member for member in current if member.get("committed") is True]
        report_count = sum(int(member.get("report_count", 0)) for member in committed)
        state = runtime["collection_state"]
        importable = str(record["job_id"]) if state == "VERIFIED" else None
        return {
            "collection_id": record["job_id"],
            "state": state,
            "verification_state": state,
            "total_registered_packs": len(members),
            "committed_packs": len(committed),
            "active_packs": len(active),
            "failed_cancelled_packs": len(failed),
            "exact_committed_report_count": report_count,
            "importable_collection_job_id": importable,
            "verified_inbox_path": runtime.get("verified_inbox_path"),
            "members": members,
        }

    def verify(self, collection_id: str) -> Path:
        with self.registry.lock:
            record = self.registry.get(collection_id)
            if record.get("kind") != _COLLECTION_KIND:
                raise PanelJobError("COLLECTION_NOT_FOUND")
            latest = self._latest()
            if latest is None or latest.get("job_id") != collection_id:
                raise PanelJobError("COLLECTION_NOT_ACTIVE")
            runtime = deepcopy(self._runtime(record))
            revision = self._revision(runtime)
            state = runtime["collection_state"]
            if state == "VERIFIED":
                if runtime.get("import_in_progress") is not None:
                    raise PanelJobError("COLLECTION_IMPORT_IN_PROGRESS")
                path = runtime.get("verified_inbox_path")
                if isinstance(path, str) and Path(path).is_dir():
                    self._read_manifest_binding(collection_id, Path(path), runtime.get("verified_inbox_sha256"))
                    runtime["performance_v2_import_verified"] = True
                    self._persist_runtime(collection_id, runtime, expected_revision=revision)
                    return Path(path)
                raise PanelJobError("COLLECTION_VERIFIED_INBOX_UNAVAILABLE")
            if state != "OPEN":
                raise PanelJobError("COLLECTION_NOT_OPEN")
            snapshot_members = [deepcopy(member) for member in runtime["members"] if isinstance(member, dict)]
        members = [self._member_view(member) for member in snapshot_members]
        current = [member for member in members if member.get("superseded") is not True]
        active = [member for member in current if member.get("state") in _ACTIVE_STATES]
        if active:
            raise PanelJobError("COLLECTION_ACTIVE_MEMBERS:" + str(active[0].get("tester_job_id")))
        paths = [Path(member["inbox_path"]) for member in current if member.get("committed") is True]
        if not paths:
            raise PanelJobError("COLLECTION_NO_COMMITTED_MEMBERS")
        expected_member_names = [
            list(member.get("expected_strategy_names", ()))
            for member in current
            if member.get("committed") is True
        ]
        inbox = build_single_mode_collection_inbox(
            self.inbox_root,
            collection_id,
            paths,
            report_root=self.report_root,
            trusted_strategy_root=self.trusted_strategy_root,
            expected_member_names=expected_member_names,
        )
        digest = self._read_manifest_binding(collection_id, inbox)
        with self.registry.lock:
            current = self.registry.get(collection_id)
            latest = self._latest()
            current_runtime = self._runtime(current)
            if latest is None or latest.get("job_id") != collection_id or current_runtime.get("collection_state") != "OPEN":
                raise PanelJobError("COLLECTION_CHANGED_DURING_VERIFY")
            runtime = deepcopy(current_runtime)
            runtime["collection_state"] = "VERIFIED"
            runtime["verified_inbox_path"] = str(inbox)
            runtime["verified_inbox_sha256"] = digest
            runtime["performance_v2_import_verified"] = True
            runtime["inbox_path"] = str(inbox)
            self._persist_runtime(collection_id, runtime, expected_revision=revision)
        return inbox

    def clear(self, collection_id: str) -> dict[str, object]:
        with self.registry.lock:
            record = self.registry.get(collection_id)
            if record.get("kind") != _COLLECTION_KIND:
                raise PanelJobError("COLLECTION_NOT_FOUND")
            latest = self._latest()
            if latest is None or latest.get("job_id") != collection_id:
                raise PanelJobError("COLLECTION_NOT_ACTIVE")
            runtime = self._runtime(record)
            if runtime["collection_state"] == "IMPORTED":
                raise PanelJobError("COLLECTION_ALREADY_IMPORTED")
            if runtime.get("import_in_progress") is not None:
                raise PanelJobError("COLLECTION_IMPORT_IN_PROGRESS")
            revision = self._revision(runtime)
            runtime["collection_state"] = "CLEARED"
            runtime["performance_v2_import_verified"] = False
            self._persist_runtime(collection_id, runtime, expected_revision=revision)
        return self.status()

    def mark_imported(self, collection_id: str) -> None:
        with self.registry.lock:
            record = self.registry.get(collection_id)
            if record.get("kind") != _COLLECTION_KIND:
                raise PanelJobError("COLLECTION_NOT_FOUND")
            runtime = self._runtime(record)
            claim = runtime.get("import_in_progress")
            if not isinstance(claim, str):
                raise PanelJobError("COLLECTION_IMPORT_NOT_CLAIMED")
        self.finish_import(collection_id, claim, committed=True)


__all__ = ["PanelReportCollection"]
