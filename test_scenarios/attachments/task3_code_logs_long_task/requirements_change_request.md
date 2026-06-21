# Change Request

        The importer currently fails the whole job when one row is malformed.
        Please update it so invalid rows are collected into `errors` and valid rows continue.

        Acceptance criteria:
        - Missing required fields should not crash the import.
        - Invalid JSON should not crash the import.
        - Email should be normalized to lowercase.
        - Tests should pass.
        - Add a short summary of the root cause and fix.
