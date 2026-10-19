"""Stage (ПД / РД / ИД) of a file uploaded through the interface, read from its own name and folders.

Deterministic and conservative: whole words only («ПД», «РД», «ИД», PD/RD/ID, «Проектная…», «Рабочая…»,
«Исполнительная…»), underscores, dots, hyphens and spaces are separators. A component that names exactly one stage gives
that stage; a component that names several («ПД_РД», «Рабочая и исполнительная») gives none and a warning -- the
inspector decides, nothing is guessed. Folder names come before the file name (outermost first), so a ZIP's stage
folder wins over a word in a file name. Used only as a default for uploads that carry no registry stage and sit in no
stage folder (see `batch_package.build_plan(stage_fallback=...)`); the registry and the package path are never
overridden by it."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_WORD_RE = re.compile(r"[0-9a-zа-яё]+")
_EXACT = {
    "PD": {"пд", "pd"},
    "RD": {"рд", "rd"},
    "ID": {"ид", "id"},
}
_CYRILLIC_WITH_NUMBER = re.compile(r"^(пд|рд|ид)\d{1,2}$")  # «РД1», «ИД2»; not Latin «id123»
_PREFIX = {
    "PD": ("проектн",),
    "RD": ("рабоч",),
    "ID": ("исполнител",),
}
_EXTRA = {"ID": {"asbuilt", "built"}}

SOURCE_FILENAME = "FILENAME"
SOURCE_FOLDER = "FOLDER"
SOURCE_LABELS = {
    "REGISTRY": "по реестру",
    "PACKAGE_PATH": "по папке пакета",
    SOURCE_FOLDER: "по папке",
    SOURCE_FILENAME: "по имени файла",
}


@dataclass(frozen=True)
class StageGuess:
    stage: str | None  # "PD" | "RD" | "ID" | None
    source: str | None  # SOURCE_FILENAME | SOURCE_FOLDER | None
    warning: str | None = None
    component: str | None = None


def stages_named_in(text: object) -> set[str]:
    stages: set[str] = set()
    for word in _WORD_RE.findall(str(text or "").casefold()):
        for stage, exact in _EXACT.items():
            if word in exact:
                stages.add(stage)
        if _CYRILLIC_WITH_NUMBER.match(word):
            stages.add({"пд": "PD", "рд": "RD", "ид": "ID"}[word[:2]])
        for stage, prefixes in _PREFIX.items():
            if word.startswith(prefixes):
                stages.add(stage)
        for stage, extra in _EXTRA.items():
            if word in extra:
                stages.add(stage)
    return stages


def stage_from_name(path: object) -> StageGuess:
    """`path`: a file name or a package-relative path. Folders are read outermost first, the file name (without its
    extension) last."""
    parts = PurePosixPath(str(path or "").replace("\\", "/")).parts
    if not parts:
        return StageGuess(None, None)
    folders, name = parts[:-1], PurePosixPath(parts[-1]).stem
    for index, component in enumerate([*folders, name]):
        found = stages_named_in(component)
        if not found:
            continue
        is_name = index == len(folders)
        if len(found) == 1:
            return StageGuess(next(iter(found)), SOURCE_FILENAME if is_name else SOURCE_FOLDER, None, component)
        labels = "/".join(stage for stage in ("PD", "RD", "ID") if stage in found)
        return StageGuess(
            None, None,
            f"В названии «{component}» указано несколько стадий ({labels}): стадия не определена, задайте её в реестре "
            "или переименуйте файл",
            component,
        )
    return StageGuess(None, None)
