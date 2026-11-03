import pytest

from app.db.migration_diagnostics import migration_exception_summary


def test_migration_diagnostic_reports_type_and_sqlstate_without_exception_text():
    class DriverError(Exception):
        sqlstate = "42501"

    class WrappedDatabaseError(Exception):
        def __init__(self):
            super().__init__(
                "postgresql://test-user:private-password@example.invalid/db "
                "api_key=private-key patient=private-name"
            )
            self.orig = DriverError("sensitive driver detail")

    summary = migration_exception_summary(WrappedDatabaseError())

    assert "WrappedDatabaseError" in summary
    assert "DriverError" in summary
    assert "sqlstate=42501" in summary
    for secret in ("private-password", "private-key", "private-name", "sensitive"):
        assert secret not in summary


@pytest.mark.parametrize("link", ["cause", "context"])
def test_migration_diagnostic_follows_python_exception_links(link):
    class DriverError(Exception):
        sqlstate = "42501"

    class MigrationWrapperError(Exception):
        pass

    try:
        if link == "cause":
            raise MigrationWrapperError("private database URL") from DriverError(
                "sensitive driver detail"
            )
        try:
            raise DriverError("sensitive driver detail")
        except DriverError:
            raise MigrationWrapperError("private database URL")
    except MigrationWrapperError as error:
        summary = migration_exception_summary(error)

    assert "MigrationWrapperError" in summary
    assert "DriverError" in summary
    assert "sqlstate=42501" in summary
    assert "private database URL" not in summary
    assert "sensitive driver detail" not in summary
