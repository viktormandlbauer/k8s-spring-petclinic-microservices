#!/usr/bin/env python3
"""Regenerate the Helm chart's MySQL init scripts in helm/petclinic/files/initdb/.

The MySQL schema is shared: visits.pet_id is a foreign key onto pets.id, which
customers-service owns. So the files must be applied in dependency order, which
is what the numeric prefixes below encode (MySQL's entrypoint runs
/docker-entrypoint-initdb.d in alphabetical order). The chart packs these files
into the <release>-mysql-initdb ConfigMap.

Run after editing any db/mysql/*.sql and commit the result; --check fails if the
chart files are stale. The scripts only run when MySQL initialises an empty data
directory - an existing PVC keeps its schema.
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "helm", "petclinic", "files", "initdb")

ORDER = [
    ("01-customers-schema.sql", "spring-petclinic-customers-service/src/main/resources/db/mysql/schema.sql"),
    ("02-vets-schema.sql",      "spring-petclinic-vets-service/src/main/resources/db/mysql/schema.sql"),
    ("03-visits-schema.sql",    "spring-petclinic-visits-service/src/main/resources/db/mysql/schema.sql"),
    ("04-customers-data.sql",   "spring-petclinic-customers-service/src/main/resources/db/mysql/data.sql"),
    ("05-vets-data.sql",        "spring-petclinic-vets-service/src/main/resources/db/mysql/data.sql"),
    ("06-visits-data.sql",      "spring-petclinic-visits-service/src/main/resources/db/mysql/data.sql"),
]


def render(rel):
    # utf-8-sig: some of these files carry a BOM
    body = io.open(os.path.join(ROOT, rel), encoding="utf-8-sig").read().rstrip("\n")
    if "USE petclinic" not in body:
        body = "USE petclinic;\n\n" + body
    return body + "\n"


def main():
    check = sys.argv[1:] == ["--check"]
    stale = []
    for key, rel in ORDER:
        path = os.path.join(TARGET, key)
        text = render(rel)
        current = io.open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if current == text:
            continue
        stale.append(key)
        if not check:
            io.open(path, "w", encoding="utf-8", newline="").write(text)
    if check and stale:
        sys.exit("stale chart init scripts: %s; run scripts/generate_mysql_initdb.py" % ", ".join(stale))
    print("regenerated: " + ", ".join(stale) if stale and not check else "helm/petclinic/files/initdb is up to date")


if __name__ == "__main__":
    main()
