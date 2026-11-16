// Biometric Hub on the Employee form:
//  * "Device Access" — which machines / mobile app the employee may use;
//  * the photo and name the attendance machine has for each of their IDs, fetched
//    from the office PC when the ID is set or changed, so HR can check it is the
//    right person.
const BH_DU = "biometric_hub.biometric_hub.doctype.biometric_device_user.biometric_device_user";

frappe.ui.form.on("Employee", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frappe.model.can_read("Biometric Employee Access")) {
			frm.add_custom_button(
				__("Device Access"),
				() => {
					frappe.db.exists("Biometric Employee Access", frm.doc.name).then((exists) => {
						if (exists) {
							frappe.set_route("Form", "Biometric Employee Access", frm.doc.name);
						} else {
							frappe.new_doc("Biometric Employee Access", { employee: frm.doc.name });
						}
					});
				},
				__("Attendance")
			);
		}

		if (frappe.model.can_read("Biometric Device User")) {
			if (frappe.model.can_write("Biometric Device User")) {
				frm.add_custom_button(
					__("Fetch machine photo"),
					() => frappe.call(`${BH_DU}.fetch_for_employee`, { employee: frm.doc.name }).then(() => {
						frappe.show_alert(__("Requested. The office PC sends it on its next check-in."));
						bh_render_machine_profiles(frm);
					}),
					__("Attendance")
				);
			}
			bh_render_machine_profiles(frm);
		}

		if (frappe.model.can_read("Biometric User Sync")) bh_render_easytime(frm);
	},
	attendance_device_id(frm) {
		// The request is queued on save; say so, so HR knows to look again shortly.
		if (frm.doc.attendance_device_id) {
			frappe.show_alert(__("After saving, the machine photo for ID {0} is fetched from the office PC.", [frm.doc.attendance_device_id]));
		}
	},
});

function bh_render_machine_profiles(frm) {
	frappe.call(`${BH_DU}.employee_profiles`, { employee: frm.doc.name }).then(({ message: m }) => {
		frm.dashboard.wrapper.find(".bh-machine-profiles").remove();
		if (!m || (!m.profiles.length && !m.ids_without_profile.length)) return;
		const esc = frappe.utils.escape_html;
		const status = {
			Requested: `<span class="indicator-pill orange">${__("Waiting for office PC")}</span>`,
			Received: `<span class="indicator-pill green">${__("From machine")}</span>`,
			"Not Found": `<span class="indicator-pill red">${__("Not in attendance software")}</span>`,
			Error: `<span class="indicator-pill red">${__("Error")}</span>`,
		};
		const cards = m.profiles.map((p) => `
			<a href="/app/biometric-device-user/${encodeURIComponent(p.name)}" class="text-reset"
			   style="display:flex;gap:12px;align-items:center;padding:8px 12px;border:1px solid var(--border-color);border-radius:10px;min-width:240px">
				${p.photo
					? `<img src="${esc(p.photo)}" style="width:56px;height:68px;object-fit:cover;border-radius:8px">`
					: `<div style="width:56px;height:68px;border-radius:8px;border:1px dashed var(--border-color)"></div>`}
				<div>
					<div><b>ID ${esc(p.name)}</b></div>
					<div class="text-muted small">${esc(p.name_on_device || __("Name not received yet"))}</div>
					<div class="mt-1">${status[p.profile_status] || ""}</div>
					${p.profile_error ? `<div class="small text-danger">${esc(p.profile_error)}</div>` : ""}
				</div>
			</a>`).join("");
		const extra = m.ids_without_profile.length
			? `<div class="text-muted small mt-2">${__("No machine profile yet for ID(s): {0}. Use Attendance → Fetch machine photo.", [esc(m.ids_without_profile.join(", "))])}</div>`
			: "";
		const off = m.enabled ? "" : `<div class="text-muted small mt-2">${__("Automatic fetching is off (Biometric Settings → Fetch Profiles from Connector).")}</div>`;
		$(`<div class="bh-machine-profiles form-dashboard-section" style="padding:12px 0">
			<div class="section-head mb-2">${__("On the attendance machine")}</div>
			<div style="display:flex;gap:12px;flex-wrap:wrap">${cards}</div>${extra}${off}
		</div>`).appendTo(frm.dashboard.wrapper);
		frm.dashboard.show();
	});
}

// Easy Time Pro push: send button + the latest result per ID.
const BH_US_E = "biometric_hub.biometric_hub.doctype.biometric_user_sync.biometric_user_sync";

function bh_render_easytime(frm) {
	frappe.call(`${BH_US_E}.employee_status`, { employee: frm.doc.name }).then(({ message: m }) => {
		frm.dashboard.wrapper.find(".bh-easytime").remove();
		if (!m || (!m.enabled && !m.rows.length)) return;
		if (m.enabled && frm.doc.status === "Active" && frappe.model.can_write("Biometric User Sync")) {
			frm.add_custom_button(__("Send to Easy Time Pro"), () =>
				frappe.call(`${BH_US_E}.push_employee`, { employee: frm.doc.name }).then(() => {
					frappe.show_alert(__("Queued. The office PC creates or updates them in Easy Time Pro on its next check-in."));
					bh_render_easytime(frm);
				}), __("Attendance"));
		}
		if (!m.rows.length) return;
		const esc = frappe.utils.escape_html;
		const colour = { Done: "green", Queued: "orange", Sent: "blue", "Dry Run": "purple", Conflict: "red", Error: "red", Skipped: "gray" };
		const lines = m.rows.map((r) => `
			<div style="display:flex;gap:10px;align-items:center;margin:4px 0">
				<a href="/app/biometric-user-sync/${encodeURIComponent(r.name)}"><b>ID ${esc(r.device_user_id)}</b></a>
				<span class="indicator-pill ${colour[r.status] || "gray"}">${__(r.status)}</span>
				<span class="text-muted small">${esc(r.action)}${r.outcome ? " · " + esc(r.outcome) : ""}${r.message ? " · " + esc(r.message) : ""}</span>
			</div>`).join("");
		$(`<div class="bh-easytime form-dashboard-section" style="padding:12px 0">
			<div class="section-head mb-2">${__("In Easy Time Pro")}</div>${lines}
		</div>`).appendTo(frm.dashboard.wrapper);
		frm.dashboard.show();
	});
}
