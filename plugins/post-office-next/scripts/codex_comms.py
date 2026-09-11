# SPDX-License-Identifier: MPL-2.0
"""Attested compatibility marker for the automatic-review client source bundle.

The vNext adapter is deliberately standalone and imports no legacy Post Office backend. This file
remains beside it because automatic-review client v0.1 attests a two-file backend bundle.
"""

BACKEND_FAMILY = "POST_OFFICE_NEXT"
