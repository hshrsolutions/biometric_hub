// Copyright (c) 2026, GroHR and contributors
// Conflict / Error / Dry Run rows: what HR can do about them.
const BH_US = "biometric_hub.biometric_hub.doctype.biometric_user_sync.biometric_user_sync";

frappe.ui.form.on("Biometric User Sync", {
	refresh(frm) {
		const colour = { Done: "green", Queued: "orange", Sent: "blue", "Dry Run": "purple", Conflict: "red", Error: "red", Skipped: "gray" };
		frm.page.set_indicator(__(frm.doc.status), colour[frm.doc.status] || "gray");
		if (!frm.perm[0].write) return;

		if (frm.doc.status === "Conflict") {
			frm.set_intro(__("Easy Time Pro already has a user with ID {0} called {1}. If that really is {2}, use Overwrite. Otherwise fix the ID on the employee — nothing was changed.",
				[frm.doc.device_user_id, `<b>${frappe.utils.escape_html(frm.doc.name_in_easytime || "?")}</b>`, `<b>${frappe.utils.escape_html(frm.doc.employee_name || frm.doc.employee)}</b>`]), "red");
		}
		if (["Conflict", "Error", "Dry Run"].includes(frm.doc.status) && frm.doc.action === "Add / Update") {
			frm.add_custom_button(__("Overwrite"), () => frappe.confirm(
				__("Replace the Easy Time Pro user {0} ({1}) with {2}? Their punches will then count for {2}.",
					[frm.doc.device_user_id, frm.doc.name_in_easytime || "?", frm.doc.employee_name]),
				() => frappe.call(`${BH_US}.overwrite`, { name: frm.doc.name }).then(() => frm.reload_doc())));
		}
		if (["Error", "Skipped", "Dry Run", "Conflict"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry"), () =>
				frappe.call(`${BH_US}.retry`, { name: frm.doc.name }).then(() => frm.reload_doc()));
		}
	},
});
