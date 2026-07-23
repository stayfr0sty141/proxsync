"""Settings use cases.

The Pydantic section model is the source of truth. Rows that do not exist fall back to model
defaults, so a fresh install works before anything is seeded and adding a field in a later
release does not require a data migration.

Updates are a **merge**: the caller sends only the fields it wants to change. That makes a
partial save from the UI safe and stops a stale form from silently reverting a field someone
else just changed.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.core.crypto import SecretBox
from app.core.errors import SchemaValidationFailed, ValidationFailed
from app.core.logging import logger
from app.db.models.system import Setting
from app.repositories.settings_repository import SqlAlchemySettingsRepository, unwrap
from app.schemas.enums import SettingsSection, SettingValueType
from app.schemas.settings import (
    SECRET_UNCHANGED,
    SECTION_MODELS,
    AllSettingsResponse,
    SecretStatus,
    SectionModel,
    SectionResponse,
    secret_fields,
)


def _infer_value_type(value: Any) -> SettingValueType:
    if isinstance(value, bool):
        return SettingValueType.BOOL
    if isinstance(value, int):
        return SettingValueType.INT
    if isinstance(value, (dict, list)):
        return SettingValueType.JSON
    return SettingValueType.STRING


class SettingsService:
    def __init__(self, *, repository: SqlAlchemySettingsRepository, secret_box: SecretBox) -> None:
        self._repository = repository
        self._secrets = secret_box

    # ---- reads ---------------------------------------------------------------

    async def get_section(self, section: SettingsSection) -> SectionModel:
        """Fully materialised section, secrets decrypted. For internal callers only."""
        model_cls = SECTION_MODELS[section]
        rows = await self._repository.list_section(section)
        secrets = secret_fields(section)

        values: dict[str, Any] = {}
        for row in rows:
            if row.key not in model_cls.model_fields:
                continue  # a field removed in a later release; ignored, not an error
            stored = unwrap(row.value)
            if row.key in secrets and isinstance(stored, str) and stored:
                stored = self._secrets.decrypt(stored)
            values[row.key] = stored

        return model_cls.model_validate(values)

    async def get_section_response(self, section: SettingsSection) -> SectionResponse:
        """Section as the API returns it: secrets replaced by a configured/hint summary."""
        model = await self.get_section(section)
        secrets = secret_fields(section)
        rows = {row.key: row for row in await self._repository.list_section(section)}

        payload = model.model_dump(mode="json")
        secret_status: dict[str, SecretStatus] = {}
        for field in secrets:
            raw = payload.pop(field, None)
            row = rows.get(field)
            secret_status[field] = SecretStatus(
                configured=bool(raw),
                hint=self._secrets.hint(raw) if isinstance(raw, str) and raw else None,
                updated_at=row.updated_at.isoformat() if row is not None else None,
            )

        return SectionResponse(section=section, values=payload, secrets=secret_status)

    async def get_all(self) -> AllSettingsResponse:
        sections = [await self.get_section_response(section) for section in SECTION_MODELS]
        return AllSettingsResponse(sections=sections)

    # ---- writes --------------------------------------------------------------

    async def update_section(
        self,
        section: SettingsSection,
        payload: dict[str, Any],
        *,
        updated_by: int | None = None,
    ) -> SectionResponse:
        model_cls = SECTION_MODELS[section]
        secrets = secret_fields(section)

        unknown = set(payload) - set(model_cls.model_fields)
        if unknown:
            raise ValidationFailed(
                f"Unknown setting(s) for section '{section.value}': {', '.join(sorted(unknown))}"
            )

        current = await self.get_section(section)
        merged = self._merge(current.model_dump(), payload, secrets=secrets)
        try:
            validated = model_cls.model_validate(merged)
        except ValidationError as exc:
            # Surface field-level detail instead of a 500: these are user-entered values.
            raise SchemaValidationFailed(
                f"Invalid values for section '{section.value}'",
                extra={
                    "errors": [
                        {
                            "field": ".".join(str(part) for part in error["loc"]),
                            "message": error["msg"],
                        }
                        for error in exc.errors()
                    ]
                },
            ) from exc

        for field, value in validated.model_dump(mode="json").items():
            if field in secrets and field not in payload:
                continue  # untouched secret: leave the existing ciphertext alone
            await self._persist_field(
                section, field, value, is_secret=field in secrets, updated_by=updated_by
            )

        # Field *names* only: the values may be secrets, and logs are not a place for those.
        logger.info(
            "settings_updated",
            section=section.value,
            fields=sorted(payload),
            updated_by=updated_by,
        )
        return await self.get_section_response(section)

    def _merge(
        self, current: dict[str, Any], payload: dict[str, Any], *, secrets: frozenset[str]
    ) -> dict[str, Any]:
        merged = dict(current)
        for field, value in payload.items():
            if field not in secrets:
                merged[field] = value
                continue
            resolved = _resolve_secret(value)
            if resolved is not _KEEP:
                merged[field] = resolved
        return merged

    async def _persist_field(
        self,
        section: SettingsSection,
        field: str,
        value: Any,
        *,
        is_secret: bool,
        updated_by: int | None,
    ) -> None:
        if is_secret:
            to_store = self._secrets.encrypt(value) if value else None
            value_type = SettingValueType.SECRET
        else:
            to_store = value
            value_type = _infer_value_type(value)

        await self._repository.upsert(
            section=section,
            key=field,
            value=to_store,
            value_type=value_type,
            is_secret=is_secret,
            updated_by=updated_by,
        )

    # ---- seeding -------------------------------------------------------------

    async def ensure_defaults(self, *, timezone: str | None = None) -> int:
        """Write missing defaults and repair retired unsafe values.

        Idempotent, and safe to run on every start. Existing operator choices are preserved
        except for values that a newer schema deliberately turns into a hard safety invariant.
        M5 retired the ability to disable the upload-before-delete guard; normalising that one
        legacy value here keeps upgrades bootable without ever running retention unsafely.
        """
        written = 0
        for section, model_cls in SECTION_MODELS.items():
            rows = await self._repository.list_section(section)
            existing = {row.key for row in rows}
            defaults = self._section_defaults(section, model_cls, timezone)

            if section is SettingsSection.RETENTION:
                upload_guard = next(
                    (row for row in rows if row.key == "require_upload_before_delete"), None
                )
                if upload_guard is not None and unwrap(upload_guard.value) is not True:
                    await self._persist_field(
                        section,
                        "require_upload_before_delete",
                        True,
                        is_secret=False,
                        updated_by=None,
                    )
                    written += 1
                    logger.warning(
                        "unsafe_retention_setting_normalized",
                        field="require_upload_before_delete",
                    )

            for field, value in defaults.items():
                if field in existing:
                    continue
                is_secret = field in secret_fields(section)
                await self._persist_field(
                    section,
                    field,
                    None if is_secret else value,
                    is_secret=is_secret,
                    updated_by=None,
                )
                written += 1

        if written:
            logger.info("settings_defaults_seeded", count=written)
        return written

    @staticmethod
    def _section_defaults(
        section: SettingsSection, model_cls: type[SectionModel], timezone: str | None
    ) -> dict[str, Any]:
        defaults = model_cls()
        if timezone is not None and section is SettingsSection.GENERAL:
            defaults = model_cls.model_validate({**defaults.model_dump(), "timezone": timezone})
        return dict(defaults.model_dump(mode="json"))

    async def rows(self) -> list[Setting]:
        return await self._repository.list_all()


class _Keep:
    """Sentinel: this secret was not supplied, so leave the stored value untouched."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<KEEP>"


_KEEP = _Keep()


def _resolve_secret(incoming: Any) -> Any:
    """None or the sentinel keeps the stored secret; an empty string clears it."""
    if incoming is None or incoming == SECRET_UNCHANGED:
        return _KEEP
    if incoming == "":
        return None
    return incoming
