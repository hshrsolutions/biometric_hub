"""An attendance-software database (MySQL / MariaDB or SQL Server) as a source.

The vendor's software — eSSL Time Pro, eTimeTrack, BioTime, ZKTime — writes
punches into a database on the customer's own network, which this server cannot
reach. So, like WDMS, this driver is a registry entry and a configuration target:
the reading is done by the connector agent inside that network, and the database
login lives only in the agent's local `config.json` (`sources`), never here.
"""

from biometric_hub.drivers.wdms import WDMSDriver


class SQLDatabaseDriver(WDMSDriver):
	driver_name = "SQL Database"
	driver_version = "1.0.0"
