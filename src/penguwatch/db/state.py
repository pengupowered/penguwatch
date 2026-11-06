"""App state key-value queries for the repository."""

from typing import Dict, Optional


class StateMixin:
    # --- App State Key-Value ---

    async def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        db = await self.read_connection()
        cursor = await db.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_state(self, key: str, value: str):
        async with self.write() as db:
            await db.execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    async def delete_state(self, key: str):
        async with self.write() as db:
            await db.execute("DELETE FROM app_state WHERE key = ?", (key,))

    async def delete_states_before(self, prefix: str, value_before: str) -> int:
        """Removes the keys that start with `prefix` and hold a value below `value_before` (ISO dates compare as text)."""
        async with self.write() as db:
            cursor = await db.execute(
                "DELETE FROM app_state WHERE key LIKE ? ESCAPE '\\' AND value < ?",
                (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", value_before))
        return cursor.rowcount

    async def set_states(self, values: Dict[str, Optional[str]]) -> None:
        """Sets (or, for None, deletes) several keys in ONE write transaction: all of them change or none."""
        async with self.write() as db:
            for key, value in values.items():
                if value is None:
                    await db.execute("DELETE FROM app_state WHERE key = ?", (key,))
                else:
                    await db.execute(
                        """
                        INSERT INTO app_state (key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (key, value),
                    )
