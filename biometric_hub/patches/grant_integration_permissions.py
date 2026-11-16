"""Re-apply the least-privilege grant and settings defaults on migrate.

The migrate half of the dual path: doctype JSON carries the schema, this carries
the things JSON cannot express.
"""

from biometric_hub.setup import scaffold


def execute():
	scaffold.grant_integration_permissions()
	scaffold.seed_settings()
