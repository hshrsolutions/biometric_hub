// Bulk actions: push every active employee (with a preview first), and resend
// everything after a dry-run commissioning.
const BH_USL = "biometric_hub.biometric_hub.doctype.biometric_user_sync.biometric_user_sync";

frappe.listview_settings["Biometric User Sync"] = {
	get_indicator(doc) {
		const colour = { Done: "green", Queued: "orange", Sent: "blue", "Dry Run": "purple", Conflict: "red", Error: "red", Skipped: "gray" };
		return [__(doc.status), colour[doc.status] || "gray", `status,=,${doc.status}`];
	},
	onload(list) {
		if (!frappe.model.can_write("Biometric User Sync")) return;

		list.page.add_inner_button(__("Push all active employees"), () => {
			const d = new frappe.ui.Dialog({
				title: __("Push employees to Easy Time Pro"),
				fields: [
					{ fieldname: "branch", fieldtype: "Link", options: "Branch", label: __("Only this branch (optional)") },
					{ fieldname: "out", fieldtype: "HTML" },
				],
				primary_action_label: __("Preview"),
				primary_action(v) {
					frappe.call(`${BH_USL}.preview_all`, { branch: v.branch }).then(({ message: m }) => {
						const gaps = Object.entries(m.no_area_values).map(([k, n]) => `${frappe.utils.escape_html(k)} (${n})`).join(", ");
						d.fields_dict.out.$wrapper.html(`
							<p>${__("{0} active employees.", [m.employees])}</p>
							<ul>
								<li><b>${m.ids}</b> ${__("ID(s) will be queued")}</li>
								<li>${m.already_done} ${__("already in Easy Time Pro from GroHR (left alone)")}</li>
								<li>${m.no_id} ${__("employees have no biometric ID")}</li>
								<li>${m.no_area} ${__("employees have no area rule")}${gaps ? `: ${gaps}` : ""}</li>
							</ul>
							${m.enabled ? "" : `<p class="text-danger">${__("Pushing is switched off in Biometric Settings.")}</p>`}
							<p class="text-muted small">${__("A user Easy Time Pro already has under the same ID is never overwritten unless the names match; others come back as Conflict for you to check.")}</p>`);
						d.set_primary_action(__("Queue {0}", [m.ids]), () => {
							frappe.call(`${BH_USL}.push_all`, { branch: v.branch }).then(({ message: n }) => {
								d.hide();
								frappe.show_alert({ message: __("{0} queued. The office PC sends them on its next check-in.", [n]), indicator: "green" });
								list.refresh();
							});
						});
					});
				},
			});
			d.show();
		});

		list.page.add_inner_button(__("Resend all Dry Run"), () =>
			frappe.confirm(__("Queue every Dry Run row again? Do this after turning dry_run off on the office PC."), () =>
				frappe.call(`${BH_USL}.requeue_status`, { status: "Dry Run" }).then(({ message: n }) => {
					frappe.show_alert(__("{0} queued.", [n]));
					list.refresh();
				})));
	},
};
