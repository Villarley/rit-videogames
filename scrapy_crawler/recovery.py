"""One resumable repair pass without erasing ordinary request fingerprints."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from scrapy.utils.request import RequestFingerprinter

_LINKS = re.compile(r"LINKS url=(\S+) total=\d+ in_scope=(\d+) enqueued=(\d+)")
_THIN = re.compile(r"SKIP_THIN_CONTENT url=(\S+) depth=(\d+)")


class RecoveryFingerprinter(RequestFingerprinter):
    def fingerprint(self, request):
        ordinary = super().fingerprint(request)
        if request.meta.get("rit_recover_links"):
            return hashlib.sha1(b"rit-recover-domain-cap-v1\0" + ordinary).digest()
        return ordinary


def omitted_link_pages(log_path: str) -> set[str]:
    return recovery_log_context(log_path)[0]


def recovery_log_context(log_path: str) -> tuple[set[str], dict[str, int]]:
    """Select pages with partially or completely suppressed allowed links."""
    urls = set()
    thin_depths = {}
    with Path(log_path).open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            thin = _THIN.search(line)
            if thin:
                thin_depths[thin[1]] = min(int(thin[2]), thin_depths.get(thin[1], int(thin[2])))
            match = _LINKS.search(line)
            if match and int(match[2]) > int(match[3]):
                urls.add(match[1])
    return urls, thin_depths
