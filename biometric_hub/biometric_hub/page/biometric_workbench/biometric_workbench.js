// The one screen this app exists for.
//
// a customer's 255 unmapped device IDs and 38,282 failed requests a day are, here,
// 255 rows with a button next to each. Mapping one replays every punch parked
// behind it, so the attendance record is repaired rather than merely fixed
// going forward.

frappe.pages["biometric-workbench"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Biometric Workbench"),
		single_column: true,
	});

	const workbench = new BiometricWorkbench(page);
	page.set_primary_action(__("Refresh"), () => workbench.refresh(), "refresh");
	workbench.refresh();
};

class BiometricWorkbench {
	constructor(page) {
		this.page = page;
		this.body = $('<div class="bh-workbench">').appendTo(page.main);
	}

	refresh() {
		this.body.html(`<div class="text-muted p-4">${__("Loading…")}</div>`);
		Promise.all([
			frappe.call("biometric_hub.api.admin.unmapped_summary"),
			frappe.call("biometric_hub.api.admin.device_health"),
			frappe.call("biometric_hub.api.admin.recent_activity"),
		])
			.then(([unmapped, devices, activity]) => {
				this.body.empty();
				this.render_devices(devices.message || []);
				this.render_unmapped(unmapped.message || []);
				this.render_activity(activity.message || {});
			})
			.catch(() => {
				this.body.html(
					`<div class="text-danger p-4">${__("Could not load the workbench.")}</div>`
				);
			});
	}

	section(title, subtitle) {
		const wrap = $(`
			<div class="mb-5">
				<h5 class="mb-1">${frappe.utils.escape_html(title)}</h5>
				<p class="text-muted small mb-3">${frappe.utils.escape_html(subtitle || "")}</p>
				<div class="bh-body"></div>
			</div>
		`).appendTo(this.body);
		return wrap.find(".bh-body");
	}

	empty(message) {
		return `<div class="text-muted small border rounded p-3">${frappe.utils.escape_html(message)}</div>`;
	}

	// ------------------------------------------------------------- devices
	render_devices(devices) {
		const body = this.section(
			__("Devices"),
			__("A reader that has gone quiet is the failure nobody notices. Silence is shown here.")
		);
		if (!devices.length) {
			body.html(this.empty(__("No devices configured yet.")));
			return;
		}
		const colour = {
			Online: "green",
			Silent: "orange",
			Error: "red",
			Unknown: "gray",
		};
		const rows = devices
			.map(
				(d) => `
			<tr>
				<td><a href="/app/biometric-device/${encodeURIComponent(d.name)}">${frappe.utils.escape_html(d.name)}</a></td>
				<td>${frappe.utils.escape_html(d.location || "")}</td>
				<td><span class="indicator-pill ${colour[d.health_status] || "gray"}">${frappe.utils.escape_html(d.health_status || "Unknown")}</span></td>
				<td class="text-muted">${d.last_contact ? frappe.datetime.comment_when(d.last_contact) : __("never")}</td>
				<td class="text-right">${cint(d.punches_today)}</td>
			</tr>`
			)
			.join("");
		body.html(`
			<table class="table table-sm border">
				<thead><tr>
					<th>${__("Device")}</th><th>${__("Location")}</th><th>${__("Health")}</th>
					<th>${__("Last contact")}</th><th class="text-right">${__("Punches today")}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table>`);
	}

	// ------------------------------------------------------------ unmapped
	render_unmapped(rows) {
		const body = this.section(
			__("Unmapped punches"),
			__("Real people, punching, whose device ID matches no employee. Nothing here is lost — map it and every parked punch is replayed.")
		);
		if (!rows.length) {
			body.html(this.empty(__("Nothing unmapped. Every punch found its employee.")));
			return;
		}

		const table = $(`
			<table class="table table-sm border">
				<thead><tr>
					<th>${__("Device User ID")}</th><th>${__("Device")}</th>
					<th class="text-right">${__("Punches")}</th>
					<th>${__("First seen")}</th><th>${__("Last seen")}</th><th></th>
				</tr></thead>
				<tbody></tbody>
			</table>`).appendTo(body);

		const tbody = table.find("tbody");
		rows.forEach((row) => {
			const tr = $(`
				<tr>
					<td><b>${frappe.utils.escape_html(row.device_user_id)}</b></td>
					<td>${frappe.utils.escape_html(row.device || __("(unknown device)"))}</td>
					<td class="text-right">${cint(row.punches)}</td>
					<td class="text-muted small">${frappe.datetime.str_to_user(row.first_punch)}</td>
					<td class="text-muted small">${frappe.datetime.str_to_user(row.last_punch)}</td>
					<td class="text-right"></td>
				</tr>`).appendTo(tbody);

			$(`<button class="btn btn-xs btn-primary">${__("Map to employee")}</button>`)
				.appendTo(tr.find("td:last"))
				.on("click", () => this.map(row));
		});
	}

	map(row) {
		frappe.prompt(
			[
				{
					fieldname: "employee",
					label: __("Employee"),
					fieldtype: "Link",
					options: "Employee",
					reqd: 1,
					get_query: () => ({ filters: { status: "Active" } }),
				},
				{
					fieldname: "scope",
					label: __("Applies to"),
					fieldtype: "Select",
					options: [
						{ value: "global", label: __("Every device") },
						{ value: "device", label: __("Only {0}", [row.device || "this device"]) },
					],
					default: "global",
					description: __("Use 'Every device' unless this ID means different people on different readers."),
				},
			],
			(values) => {
				frappe
					.call("biometric_hub.api.admin.map_unmapped", {
						device_user_id: row.device_user_id,
						employee: values.employee,
						device: values.scope === "device" ? row.device : null,
					})
					.then((r) => {
						const out = r.message || {};
						frappe.show_alert(
							{
								message: __("{0} punches replayed, {1} now checked in.", [
									cint(out.replayed),
									cint(out.mapped),
								]),
								indicator: out.mapped ? "green" : "orange",
							},
							7
						);
						this.refresh();
					});
			},
			__("Map device user ID {0}", [row.device_user_id]),
			__("Map and replay")
		);
	}

	// ------------------------------------------------------------ activity
	render_activity(activity) {
		const body = this.section(__("Last 24 hours"), "");
		const tile = (label, value, colour) => `
			<div class="col">
				<div class="border rounded p-3 text-center">
					<div class="h4 mb-0 text-${colour}">${cint(value)}</div>
					<div class="text-muted small">${frappe.utils.escape_html(label)}</div>
				</div>
			</div>`;
		body.html(`
			<div class="row">
				${tile(__("Mapped"), activity.mapped, "success")}
				${tile(__("Unmapped"), activity.unmapped, "warning")}
				${tile(__("Blocked"), activity.blocked, "warning")}
				${tile(__("Duplicates"), activity.duplicates, "muted")}
				${tile(__("Errors"), activity.errors, "danger")}
			</div>`);
	}
}
