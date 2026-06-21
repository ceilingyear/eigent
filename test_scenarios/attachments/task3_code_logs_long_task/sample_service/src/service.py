import json
        import time


        def parse_user_line(line):
            data = json.loads(line)
            return {
                "id": data["id"],
                "email": data["email"].lower(),
                "plan": data.get("plan", "free"),
            }


        def import_users(lines):
            users = []
            errors = []
            for index, line in enumerate(lines):
                user = parse_user_line(line)
                users.append(user)
            return {"users": users, "errors": errors}


        def retry_request(fn, attempts=3):
            last_error = None
            for attempt in range(attempts):
                try:
                    return fn()
                except Exception as exc:
                    last_error = exc
                    time.sleep(attempt)
            raise last_error
