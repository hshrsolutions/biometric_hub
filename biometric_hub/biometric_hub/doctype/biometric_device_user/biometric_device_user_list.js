// Upload the attendance software's employee export (+ optional photo zip) from the desk.
frappe.listview_settings["Biometric Device User"] = {
	add_fields: ["match_status", "photo"],
	get_indicator(doc) {
		const c = { Confirmed: "green", Suggested: "orange", Unchecked: "gray", "Not This Person": "red" };
		return [__(doc.match_status), c[doc.match_status] || "gray", `match_status,=,${doc.match_status}`];
	},
	onload(list) {
		if (!frappe.model.can_create("Biometric Device User")) return;
		list.page.add_inner_button(__("Import from attendance software"), () => {
			const d = new frappe.ui.Dialog({
				title: __("Import users and photos"),
				fields: [
					{ fieldname: "users", fieldtype: "Attach", label: __("Employee export (.xlsx / .csv)"), reqd: 1 },
					{ fieldname: "photos", fieldtype: "Attach", label: __("Photo zip (from export_photos)") },
				],
				primary_action_label: __("Check"),
				primary_action(v) {
					const args = { users_file_url: v.users, photos_file_url: v.photos };
					frappe.call(`biometric_hub.biometric_hub.doctype.biometric_device_user.biometric_device_user.import_files`, args)
						.then(({ message: m }) => {
							frappe.confirm(
								__("{0} new, {1} updated, {2} photos. {3} already linked, {4} suggested, {5} unknown. Import now?",
									[m.created, m.updated, m.photos, m.already_linked, m.suggested, m.unknown]),
								() => frappe.call(`biometric_hub.biometric_hub.doctype.biometric_device_user.biometric_device_user.import_files`,
									{ ...args, apply: 1 }).then(() => { d.hide(); list.refresh(); })
							);
						});
				},
			});
			d.show();
		});
	},
};
