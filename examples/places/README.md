# Sample place lists

The same nine places in each format, for trying **Compose → Geographic constraint → Places → Import list…**:

| File | What it shows |
|---|---|
| `places_latlon.csv` | Separate `Latitude` / `Longitude` columns, radius in metres (`Radius (m)`), and one unreadable row (reported by line number) |
| `places_mixed_formats.csv` | One `location` column mixing decimal, MGRS, degrees-minutes-seconds, UTM and compact DMS; `;`-separated; radius with units (`100m`, `1.0 km`) |
| `places.geojson` | Points with `name`, `radius_m`, `iso_a2`, `description`; one polygon, which is skipped |
| `places.kml` / `places.kmz` | Placemarks with ExtendedData `radius_km` and `country`; one LineString, which is skipped |

With the default countries (all except DE, FR, IT), reviewing them with **A** should give:

| Place | Result |
|---|---|
| New York office, Boston steering committee, London desk, Istanbul office, Ankara branch, Kabul compound | Approved |
| Berlin supplier | Refused: Germany isn't permitted (excluded for GDPR) |
| Thessaloniki port (mislabelled) | Refused: labelled TR, but the point is in Greece |
| Offshore platform | Refused: the point is at sea, not inside any country |

New York, Boston and London are next to practice records (rec-001, rec-008, rec-003), so a warrant with those places and the term PROJECT_CEDAR returns only the records inside its circles.
