# -*- coding: utf-8 -*-
"""Where the tests/tools find an EndNote library on this machine.

The app itself never needs this (it remembers libraries in its own config).
For tests and dev tools, resolution order is:

1. environment variable ``ENDNOTE_LIBRARY`` (path to a ``.enl``/``.enlx``)
2. the first library saved by the GUI in
   ``%APPDATA%\\endnote-pdf-exporter\\config.json``
   (older builds used ``%APPDATA%\\EndNote2ima``, also checked)
"""

import json
import os

APP_NAME = "endnote-pdf-exporter"
LEGACY_APP_NAME = "EndNote2ima"


def _appdata():
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def config_dir():
    return os.path.join(_appdata(), APP_NAME)


def config_library():
    for name in (APP_NAME, LEGACY_APP_NAME):
        try:
            with open(os.path.join(_appdata(), name, "config.json"),
                      encoding="utf-8-sig") as fh:
                data = json.load(fh)
            libraries = data.get("libraries") or []
            if libraries:
                return libraries[0]
        except (OSError, ValueError):
            continue
    return ""


def default_library():
    return os.environ.get("ENDNOTE_LIBRARY") or config_library()


def default_sample_enlx():
    """EndNote ships a sample compressed library; used by the .enlx test."""
    explicit = os.environ.get("ENDNOTE_SAMPLE_ENLX")
    if explicit:
        return explicit
    public = os.environ.get("PUBLIC") or r"C:\Users\Public"
    return os.path.join(public, "Documents", "EndNote", "Examples", "Sample_Library_20.enlx")
