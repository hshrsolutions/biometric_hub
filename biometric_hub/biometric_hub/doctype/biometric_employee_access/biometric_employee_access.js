// Copyright (c) 2026, GroHR and contributors
// For license information, please see license.txt

frappe.ui.form.on("Biometric Employee Access", {
	refresh(frm) {
		frm.set_intro(
			frm.doc.access_mode === "Allowed Devices Only"
				? __("Punches on any device not listed below are held as Blocked. Adding the device releases them.")
				: __("This employee may punch on any device. Choose 'Allowed Devices Only' to restrict them."),
			frm.doc.access_mode === "Allowed Devices Only" ? "orange" : "blue"
		);
		if (!frm.is_new()) {
			frm.add_custom_button(__("Blocked Punches"), () =>
				frappe.set_route("List", "Biometric Punch", { employee: frm.doc.employee, status: "Blocked" })
			);
			frm.add_custom_button(__("Employee"), () => frappe.set_route("Form", "Employee", frm.doc.employee));
		}
		render_overview(frm);
	},
	employee: render_overview,
	access_mode(frm) {
		frm.trigger("refresh");
	},
});

function render_overview(frm) {
	const wrapper = frm.get_field("enrolments_html").$wrapper;
	if (!frm.doc.employee) {
		wrapper.html("");
		return;
	}
	frappe
		.call("biometric_hub.biometric_hub.doctype.biometric_employee_access.biometric_employee_access.employee_overview", {
			employee: frm.doc.employee,
		})
		.then(({ message }) => {
			const esc = frappe.utils.escape_html;
			const ids = message.enrolments.length
				? message.enrolments
						.map(
							(e) => `<tr>
								<td><a href="/app/biometric-enrolment/${e.name}">${esc(e.device_user_id)}</a></td>
								<td>${e.device ? esc(e.device) : `<span class="text-muted">${__("Every device")}</span>`}</td>
								<td>${e.enabled ? __("Yes") : `<span class="text-danger">${__("No")}</span>`}</td>
							</tr>`
						)
						.join("")
				: `<tr><td colspan="3" class="text-muted">${__("No device ID on file.")}</td></tr>`;
			const punches = message.punches.length
				? message.punches
						.map(
							(p) => `<tr>
								<td>${esc(p.device || "-")}</td>
								<td>${esc(__(p.status))}</td>
								<td>${p.n}</td>
								<td>${p.last_punch ? frappe.datetime.str_to_user(p.last_punch) : ""}</td>
							</tr>`
						)
						.join("")
				: `<tr><td colspan="4" class="text-muted">${__("No punches in the last 30 days.")}</td></tr>`;
			wrapper.html(`
				<div class="row">
					<div class="col-md-5">
						<h6>${__("Device IDs")}</h6>
						<table class="table table-sm table-bordered">
							<thead><tr><th>${__("ID")}</th><th>${__("Device")}</th><th>${__("Enabled")}</th></tr></thead>
							<tbody>${ids}</tbody>
						</table>
					</div>
					<div class="col-md-7">
						<h6>${__("Punches, last 30 days")}</h6>
						<table class="table table-sm table-bordered">
							<thead><tr><th>${__("Device")}</th><th>${__("Status")}</th><th>${__("Count")}</th><th>${__("Last")}</th></tr></thead>
							<tbody>${punches}</tbody>
						</table>
					</div>
				</div>`);
		});
}
