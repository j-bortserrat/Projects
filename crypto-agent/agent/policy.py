"""Versionado de la política (umbrales ajustables). Cada cambio es un fichero nuevo, el
anterior se conserva para poder volver atrás, y nada se activa sin una orden explícita."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .config import PolicyCfg


class PolicyStore:
    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "policies")
        os.makedirs(self.dir, exist_ok=True)
        self.index_path = os.path.join(self.dir, "index.json")

    def _index(self) -> dict:
        if not os.path.exists(self.index_path):
            return {"active": None, "history": []}
        with open(self.index_path) as fh:
            return json.load(fh)

    def _save_index(self, idx: dict) -> None:
        with open(self.index_path, "w") as fh:
            json.dump(idx, fh, indent=2)

    def _path(self, version: int) -> str:
        return os.path.join(self.dir, f"v{version:04d}.json")

    def versions(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.startswith("v") and name.endswith(".json"):
                with open(os.path.join(self.dir, name)) as fh:
                    out.append(json.load(fh))
        return out

    def get(self, version: int) -> dict:
        with open(self._path(version)) as fh:
            return json.load(fh)

    def _write(self, policy: PolicyCfg, reason: str, evidence: dict | None) -> int:
        version = max([v["version"] for v in self.versions()], default=0) + 1
        with open(self._path(version), "w") as fh:
            json.dump({"version": version, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "reason": reason, "evidence": evidence or {}, "policy": policy.model_dump()},
                      fh, indent=2, ensure_ascii=False)
        return version

    def active(self, default: PolicyCfg) -> tuple[int, PolicyCfg]:
        """Política en vigor. La primera vez registra la de la configuración como versión 1."""
        idx = self._index()
        if idx["active"] is None:
            idx["active"] = self._write(default, "política inicial (configuración)", None)
            idx["history"] = [idx["active"]]
            self._save_index(idx)
        return idx["active"], PolicyCfg.model_validate(self.get(idx["active"])["policy"])

    def propose(self, policy: PolicyCfg, reason: str, evidence: dict | None = None) -> int:
        """Guarda una candidata. NO la activa."""
        return self._write(policy, reason, evidence)

    def promote(self, version: int) -> int:
        PolicyCfg.model_validate(self.get(version)["policy"])  # falla si la versión no existe o no es válida
        idx = self._index()
        idx["active"] = version
        idx["history"].append(version)
        self._save_index(idx)
        return version

    def rollback(self) -> int:
        idx = self._index()
        if len(idx["history"]) < 2:
            raise ValueError("no hay una versión anterior a la que volver")
        idx["history"].pop()
        idx["active"] = idx["history"][-1]
        self._save_index(idx)
        return idx["active"]
