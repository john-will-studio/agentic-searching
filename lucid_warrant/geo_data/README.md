# Country borders

`borders_10m.json.gz`: Natural Earth 1:10m Cultural Vectors, Admin 0 – Countries, version 5.1.2
(https://www.naturalearthdata.com/, public domain). Reduced to each country's ISO 3166-1 alpha-2 code
(`ISO_A2_EH`), English name and polygons, with coordinates rounded to 4 decimal places (about 11 m).
Areas without an ISO code (for example Northern Cyprus, Somaliland, UN buffer zones) are left out.

Used to check that a place's coordinates are inside the country chosen for it. Borders and coastlines are
accurate to roughly a kilometre.
