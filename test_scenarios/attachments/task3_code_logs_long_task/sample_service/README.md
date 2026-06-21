# Sample Service

        This is a small intentionally flawed service used to test long-task code analysis.
        It exposes a user import function and a basic retry helper.

        Expected behavior:
        - Import users from JSON lines.
        - Skip invalid rows without crashing.
        - Retry transient network errors up to 3 times.
        - Write a summary report.
