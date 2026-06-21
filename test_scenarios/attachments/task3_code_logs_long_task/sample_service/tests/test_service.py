import pytest
        from src.service import import_users, retry_request


        def test_import_users_skips_invalid_rows():
            lines = [
                '{"id": "u1", "email": "A@EXAMPLE.COM", "plan": "pro"}',
                '{"id": "u2", "plan": "free"}',
                'not-json',
            ]
            result = import_users(lines)
            assert len(result["users"]) == 1
            assert len(result["errors"]) == 2
            assert result["users"][0]["email"] == "a@example.com"


        def test_retry_request_eventually_succeeds():
            calls = {"count": 0}

            def flaky():
                calls["count"] += 1
                if calls["count"] < 3:
                    raise RuntimeError("temporary")
                return "ok"

            assert retry_request(flaky, attempts=3) == "ok"
