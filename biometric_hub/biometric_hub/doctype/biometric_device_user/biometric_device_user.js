// Copyright (c) 2026, GroHR and contributors
// For license information, please see license.txt

const BDU = "biometric_hub.biometric_hub.doctype.biometric_device_user.biometric_device_user";

frappe.ui.form.on("Biometric Device User", {
	refresh(frm) {
		render_compare(frm);
		if (frm.is_new() || !frm.perm[0].write) return;
		frm.add_custom_button(__("Fetch from office PC"), () =>
			frappe.call(`${BDU}.fetch_profile`, { device_user: frm.doc.name }).then(() => {
				frappe.show_alert(__("Requested. The office PC sends it on its next check-in."));
				frm.reload_doc();
			})
		);
		if (frm.doc.profile_status === "Requested") {
			frm.dashboard.set_headline(__("Waiting for the office PC to send this user's name and photo."), "orange");
		}
		if (frm.doc.match_status !== "Confirmed") {
			frm.add_custom_button(__("Confirm: this is the employee"), () => {
				if (!frm.doc.employee) {
					frappe.msgprint(__("Choose the employee first."));
					return;
				}
				frappe.confirm(
					__("Link machine ID {0} to {1} ({2}) and correct their past attendance?", [
						frm.doc.device_user_id, frm.doc.employee_name || "", frm.doc.employee,
					]),
					() => frappe.call(`${BDU}.confirm`, { device_user: frm.doc.name, employee: frm.doc.employee })
						.then(({ message }) => {
							frappe.show_alert({ message: __("Linked. {0} past punches repaired.", [message.mapped]), indicator: "green" });
							frm.reload_doc();
						})
				);
			}).addClass("btn-primary");
			frm.add_custom_button(__("Not this person"), () =>
				frappe.call(`${BDU}.reject`, { device_user: frm.doc.name }).then(() => frm.reload_doc())
			);
		}
	},
	employee(frm) {
		setTimeout(() => render_compare(frm), 400);
	},
});

function render_compare(frm) {
	const esc = frappe.utils.escape_html;
	const box = (title, url, caption) => `
		<div style="flex:1;min-width:200px;text-align:center">
			<div class="text-muted small mb-2">${title}</div>
			${url
				? `<img src="${esc(url)}" style="width:220px;height:260px;object-fit:cover;border-radius:12px;border:1px solid var(--border-color)">`
				: `<div style="width:220px;height:260px;margin:auto;border-radius:12px;border:1px dashed var(--border-color);display:flex;align-items:center;justify-content:center" class="text-muted">${__("No photo")}</div>`}
			<div class="mt-2"><b>${esc(caption || "")}</b></div>
		</div>`;
	frm.get_field("compare_html").$wrapper.html(`
		<div style="display:flex;gap:24px;flex-wrap:wrap;justify-content:center;margin:8px 0 16px">
			${box(__("On the attendance machine"), frm.doc.photo, `${frm.doc.name_on_device || ""} · ID ${frm.doc.device_user_id || ""}`)}
			${box(__("In GroHR"), frm.doc.employee_photo, frm.doc.employee ? `${frm.doc.employee_name || ""} · ${frm.doc.employee}` : __("No employee chosen"))}
		</div>`);
}
