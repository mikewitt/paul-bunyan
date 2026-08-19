_COLUMNS = ("a", "b")
_COLS = ", ".join(_COLUMNS)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)
sql = f"INSERT INTO records ({_COLS}) VALUES ({_PLACEHOLDERS})"  # nosec B608
