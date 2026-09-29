#!/bin/sh
# Dev helper: run SQL against the local Atlas DB as the owner role.
PGPASSWORD=atlas exec psql -h localhost -p 55433 -U atlas atlas "$@"
