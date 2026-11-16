// Copyright (c) 2026, GroHR and contributors
// For license information, please see license.txt

frappe.ui.form.on("Biometric Device", {
	refresh(frm) {
		if (frm.is_new()) return;

		frappe.db.get_value("Biometric Driver", frm.doc.driver, ["supports_pull", "supports_clock_sync"]).then((r) => {
			const caps = r.message || {};

			frm.add_custom_button(__("Test Connection"), () => {
				frappe.call({
					method: "biometric_hub.api.admin.test_device",
					args: { device: frm.doc.name },
					freeze: true,
					freeze_message: __("Reaching the device…"),
				}).then((res) => {
					const out = res.message || {};
					frappe.msgprint({
						title: out.ok ? __("Connected") : __("Could not connect"),
						message: frappe.utils.escape_html(out.message || ""),
						indicator: out.ok ? "green" : "red",
					});
					frm.reload_doc();
				});
			});

			if (caps.supports_pull) {
				frm.add_custom_button(__("Sync Now"), () => {
					frappe.call({
						method: "biometric_hub.api.admin.sync_now",
						args: { device: frm.doc.name },
						freeze: true,
						freeze_message: __("Pulling punches…"),
					}).then((res) => {
						const out = res.message || {};
						frappe.msgprint({
							title: __("Sync complete"),
							message: __(
								"{0} received — {1} mapped, {2} duplicates, {3} unmapped, {4} errors.",
								[out.received, out.mapped, out.duplicates, out.unmapped, out.errors]
							),
							indicator: out.unmapped || out.errors ? "orange" : "green",
						});
						frm.reload_doc();
					});
				});
			}
		});

		// Health is the whole point of the device record: make it impossible to miss.
		const colour = { Online: "green", Silent: "orange", Error: "red", Unknown: "gray" };
		frm.dashboard.clear_headline();
		if (frm.doc.health_status === "Silent") {
			frm.dashboard.set_headline(
				__("This device has not been heard from since {0}.", [
					frm.doc.last_contact ? frappe.datetime.str_to_user(frm.doc.last_contact) : __("ever"),
				]),
				"orange"
			);
		} else if (frm.doc.health_status === "Error" && frm.doc.last_error) {
			frm.dashboard.set_headline(frappe.utils.escape_html(frm.doc.last_error), "red");
		}
		frm.page.set_indicator(
			frm.doc.health_status || __("Unknown"),
			colour[frm.doc.health_status] || "gray"
		);
	},
});
