"""backoffice_world: the seeded synthetic payments back office the example runs on.

A synthetic back-office payments world with invented providers; no real company's data.

Only the individual transactions, end users, cases and the six provider names are invented;
the rails, calendars and FX fixings copy public rules frozen under ``sources/`` with their
URLs, dates and digests.
"""

SYNTHETIC_SENTENCE = (
    "A synthetic back-office payments world with invented providers; no real company's data."
)

FX_PROXY_SENTENCE = (
    "FX is modelled from public data: official fixings plus a modelled provider markup; "
    "no provider's real quote is used."
)

BAND_SENTENCE = (
    "Each corridor's markup band is floored at the widest quoted top-of-book spread saved for "
    "that corridor on 2026-10-12, so no modelled provider quotes tighter than the exchange did "
    "that day; a corridor without a saved probe uses the band's own lower end."
)
