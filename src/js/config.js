/**
 * Configuration for Entity Manager UI
 */
window.EntityManagerConfig = {
    // Entity types that require manual basename input (no automatic suggestions)
    entityTypesRequiringManualBasename: [
        "sensor",
        "binary_sensor",
        "button",
        "select",
        "update",
        "number",
        "input_boolean",
        "input_number",
        "input_text",
        "input_select"
    ],

    // The steps a job handler writes into its log, in the order a reader cares
    // about them. Both the progress panel and the settings log read this: kept
    // twice, one of them fell behind and showed a raw "ENABLE" to the user.
    //   label  - translation key under "jobs."
    //   icon   - shown in the settings log's filter dialog
    //   colour - suffix of the .step-* and .row-* classes
    jobSteps: {
        AREA:        {label: "step_area",        icon: "ri-map-pin-line",         colour: "renamed"},
        RENAME:      {label: "step_renamed",     icon: "ri-price-tag-3-line",     colour: "renamed"},
        ENABLE:      {label: "step_enabled",     icon: "ri-toggle-line",          colour: "enabled"},
        CARRIED:     {label: "step_carried",     icon: "ri-links-line",           colour: "carried"},
        NOT_CARRIED: {label: "step_not_carried", icon: "ri-link-unlink",          colour: "not-carried"},
        UNREACHABLE: {label: "step_unreachable", icon: "ri-error-warning-line",   colour: "unreachable"},
        BY_HAND:     {label: "step_by_hand",     icon: "ri-file-edit-line",       colour: "unreachable"},
        UNVERIFIED:  {label: "step_unverified",  icon: "ri-question-line",        colour: "unreachable"},
        ERROR:       {label: "step_error",       icon: "ri-close-circle-line",    colour: "error"}
    }
};