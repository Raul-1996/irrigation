"""Stable SQLite application metadata shared by init and backup validation."""

# ``WBIR`` encoded as a positive 32-bit integer.  SQLite stores this value in
# the database header, which lets recovery code distinguish an irrigation
# database from an unrelated SQLite file before trusting its contents.
APPLICATION_ID = 0x57424952

# This project historically used the named ``migrations`` table and therefore
# left PRAGMA user_version at SQLite's default zero.  Version 1 introduces an
# explicit header contract; additive named migrations remain independently
# tracked for backwards-compatible upgrades.
USER_VERSION = 1
