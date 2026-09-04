from fastapi import Response

from app.routes.member_subscription_owner_hotfix import member_access_states


class _Mappings:
    def all(self):
        return []


class _Result:
    def mappings(self):
        return _Mappings()


class _Session:
    def __init__(self):
        self.sql = ""

    def execute(self, statement):
        self.sql = str(statement)
        return _Result()


def test_member_list_uses_distinct_safe_ordering():
    session = _Session()
    response = Response()

    result = member_access_states(response, {"role": "owner"}, session)

    assert result == []
    assert "SELECT DISTINCT" in session.sql
    assert "ORDER BY u.display_name NULLS LAST, u.email" in session.sql
    assert "ORDER BY lower(" not in session.sql
    assert response.headers["Cache-Control"] == "no-store"
