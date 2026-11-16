"""Rename Biometric Device.last_seen to last_contact.

`frappe.get_all` silently drops any field whose name *contains* one of
`frappe.model.optional_fields` — "_seen" is one, and the test is a substring
match (`db_query.py`: `if f in fld`). So `last_seen` was removed from every ORM
read: the column held the right value and raw SQL returned it, but anything
using `get_all` saw nothing, which made a live device look like it had never
reported.

The doctype JSON already carries the new name, so by the time this runs the new
column exists. All this does is carry the data across and drop the old column.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Biometric Device"):
		return
	columns = {row[0] for row in frappe.db.sql("SHOW COLUMNS FROM `tabBiometric Device`")}
	if "last_seen" not in columns:
		return
	if "last_contact" in columns:
		frappe.db.sql(
			"UPDATE `tabBiometric Device` SET last_contact = last_seen "
			"WHERE last_contact IS NULL AND last_seen IS NOT NULL"
		)
	frappe.db.sql_ddl("ALTER TABLE `tabBiometric Device` DROP COLUMN `last_seen`")
	frappe.db.commit()
