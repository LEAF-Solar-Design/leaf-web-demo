// Keep this access foldable so flag-off builds omit the typed settings form.
export const ENV_SOLAR_SETTINGS_FORM = import.meta.env.VITE_SOLAR_SETTINGS_FORM === '1'
// Same foldable form: flag-off builds omit the guided Solar step rail.
export const ENV_SOLAR_FLOW_RAIL = import.meta.env.VITE_SOLAR_FLOW_RAIL === '1'
