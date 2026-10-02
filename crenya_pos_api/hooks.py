app_name = "crenya_pos_api"
app_title = "Crenya Pos Api"
app_publisher = "sammish"
app_description = "pos api"
app_email = "sammish.thundiyil@gmail.com"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "crenya_pos_api",
# 		"logo": "/assets/crenya_pos_api/logo.png",
# 		"title": "Crenya Pos Api",
# 		"route": "/crenya_pos_api",
# 		"has_permission": "crenya_pos_api.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/crenya_pos_api/css/crenya_pos_api.css"
# app_include_js = "/assets/crenya_pos_api/js/crenya_pos_api.js"

# include js, css files in header of web template
# web_include_css = "/assets/crenya_pos_api/css/crenya_pos_api.css"
# web_include_js = "/assets/crenya_pos_api/js/crenya_pos_api.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "crenya_pos_api/public/scss/website"

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
# app_include_icons = "crenya_pos_api/public/icons.svg"

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
# 	"methods": "crenya_pos_api.utils.jinja_methods",
# 	"filters": "crenya_pos_api.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "crenya_pos_api.install.before_install"
after_install = "crenya_pos_api.setup.install.after_install"
after_migrate = "crenya_pos_api.setup.install.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "crenya_pos_api.uninstall.before_uninstall"
# after_uninstall = "crenya_pos_api.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "crenya_pos_api.utils.before_app_install"
# after_app_install = "crenya_pos_api.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "crenya_pos_api.utils.before_app_uninstall"
# after_app_uninstall = "crenya_pos_api.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "crenya_pos_api.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

permission_query_conditions = {
	"Crenya POS Device": "crenya_pos_api.permissions.device_query_conditions",
}

has_permission = {
	"Crenya POS Device": "crenya_pos_api.permissions.device_has_permission",
}

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"User": {
		# hash a newly entered POS PIN and clear it before Frappe stores Password fields;
		# before_validate also runs when flags.ignore_validate skips validate
		"before_validate": "crenya_pos_api.sync.cashier.user_validate",
		"validate": "crenya_pos_api.sync.cashier.user_validate",
	},
	"POS Profile": {
		# tills learn about cashiers added to / removed from Applicable for Users
		"on_update": "crenya_pos_api.sync.cashier.pos_profile_on_update",
		# scale barcode rules: valid prefixes and lengths, no prefix twice
		"validate": "crenya_pos_api.sync.scale_rules.pos_profile_validate",
	},
	"Sales Invoice": {
		# till invoices: promotion names go onto the item rows after ERPNext's last validation
		"before_submit": "crenya_pos_api.sync.invoice_builder.restore_pricing_rules",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"daily": [
		"crenya_pos_api.tasks.purge_old_sync_events",
	],
}

# scheduler_events = {
# 	"all": [
# 		"crenya_pos_api.tasks.all"
# 	],
# 	"daily": [
# 		"crenya_pos_api.tasks.daily"
# 	],
# 	"hourly": [
# 		"crenya_pos_api.tasks.hourly"
# 	],
# 	"weekly": [
# 		"crenya_pos_api.tasks.weekly"
# 	],
# 	"monthly": [
# 		"crenya_pos_api.tasks.monthly"
# 	],
# }

# Testing
# -------

before_tests = "crenya_pos_api.setup.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "crenya_pos_api.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# built from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "crenya_pos_api.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["crenya_pos_api.utils.before_request"]
# after_request = ["crenya_pos_api.utils.after_request"]

# Job Events
# ----------
# before_job = ["crenya_pos_api.utils.before_job"]
# after_job = ["crenya_pos_api.utils.after_job"]

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
# 	"crenya_pos_api.auth.validate"
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
