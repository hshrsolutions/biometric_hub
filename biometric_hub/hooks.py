app_name = "biometric_hub"
app_title = "Biometric Hub"
app_publisher = "GroHR"
app_description = "Brand-agnostic biometric attendance device integration"
app_email = "support@grohr.in"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "biometric_hub",
# 		"logo": "/assets/biometric_hub/logo.png",
# 		"title": "Biometric Hub",
# 		"route": "/biometric_hub",
# 		"has_permission": "biometric_hub.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/biometric_hub/css/biometric_hub.css"
# app_include_js = "/assets/biometric_hub/js/biometric_hub.js"

# include js, css files in header of web template
# web_include_css = "/assets/biometric_hub/css/biometric_hub.css"
# web_include_js = "/assets/biometric_hub/js/biometric_hub.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "biometric_hub/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "biometric_hub/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "biometric_hub.utils.jinja_methods",
# 	"filters": "biometric_hub.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "biometric_hub.install.before_install"
# after_install = "biometric_hub.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "biometric_hub.uninstall.before_uninstall"
# after_uninstall = "biometric_hub.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "biometric_hub.utils.before_app_install"
# after_app_install = "biometric_hub.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "biometric_hub.utils.before_app_uninstall"
# after_app_uninstall = "biometric_hub.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "biometric_hub.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# HR often sets the device ID on the Employee form; treat that as a mapping.
doc_events = {
	"Employee": {
		# Employee → Biometric IDs (array of IDs, unique across employees).
		"validate": "biometric_hub.engine.employee_ids.validate",
		"on_update": [
			"biometric_hub.engine.employee_ids.sync",
			"biometric_hub.engine.enrolment.adopt_employee_device_id",
			# Left → removed from the machines; name/branch change → updated (Easy Time Pro push).
			"biometric_hub.engine.user_push.on_employee_update",
		],
	},
	# Employee device access: refuses a self check-in (the mobile app) when HR has
	# switched it off for that employee. Reads only — never writes the checkin.
	"Employee Checkin": {
		"before_insert": "biometric_hub.engine.access.check_self_service_checkin",
	},
}

# "Device Access" button on the Employee form. Additive client script only.
doctype_js = {"Employee": "public/js/employee.js"}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"biometric_hub.tasks.all"
# 	],
# 	"daily": [
# 		"biometric_hub.tasks.daily"
# 	],
# 	"hourly": [
# 		"biometric_hub.tasks.hourly"
# 	],
# 	"weekly": [
# 		"biometric_hub.tasks.weekly"
# 	],
# 	"monthly": [
# 		"biometric_hub.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "biometric_hub.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "biometric_hub.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "biometric_hub.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["biometric_hub.utils.before_request"]
# after_request = ["biometric_hub.utils.after_request"]

# Job Events
# ----------
# before_job = ["biometric_hub.utils.before_job"]
# after_job = ["biometric_hub.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"biometric_hub.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []



# ---------------------------------------------------------------------- install

after_install = "biometric_hub.setup.install.after_install"

# Drivers this app itself provides. Any app may add to this hook; the registry in
# `Biometric Driver` is reconciled from it on migrate.
biometric_drivers = [
	"biometric_hub.drivers.fake.FakeDriver",
	"biometric_hub.drivers.zkteco.ZKTecoDriver",
	"biometric_hub.drivers.adms.ADMSDriver",
	"biometric_hub.drivers.wdms.WDMSDriver",
	"biometric_hub.drivers.sqldb.SQLDatabaseDriver",
]

# Keep the Biometric Driver registry in step with whatever apps declare.
after_migrate = "biometric_hub.drivers.registry.sync"


# ------------------------------------------------------------------- scheduler

scheduler_events = {
	"hourly": ["biometric_hub.engine.health.hourly"],
	"daily": ["biometric_hub.engine.health.daily"],
}

# Employee → Biometric IDs custom field, for `bench migrate` (scaffold creates it on install).
fixtures = [{"dt": "Custom Field", "filters": [["name", "in", ["Employee-bh_biometric_ids"]]]}]
