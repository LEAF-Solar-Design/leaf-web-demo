import { z } from "zod";

/** Frozen device-tool contract. Authority is supplied by the host, never args. */
export const DEVICE_TOOL_INPUTS = {
  device_list: z.strictObject({}),
  device_install_package: z.strictObject({ device_id: z.string(), package: z.string() }),
  device_ha_step: z.strictObject({
    device_id: z.string(),
    op: z.enum(["ha.onboard", "ha.flow.start", "ha.flow.step", "ha.flow.list"]),
    args: z.record(z.string(), z.unknown()),
  }),
  device_light: z.strictObject({
    device_id: z.string(),
    action: z.enum(["turn_on", "turn_off"]),
    entity_id: z.string().regex(/^(light|group)\.[a-z0-9_]{1,64}$(?![\s\S])/),
    color: z.string().regex(/^#[0-9a-f]{6}$(?![\s\S])/).optional(),
    brightness_pct: z.number().int().min(0).max(100).optional(),
  }),
  device_light_states: z.strictObject({ device_id: z.string() }),
  device_command_status: z.strictObject({ device_id: z.string(), command_id: z.string() }),
} as const;

export const DEVICE_TOOL_OPERATIONS = {
  device_list: "list",
  device_install_package: "install_package",
  device_ha_step: "ha_step",
  device_light: "light",
  device_light_states: "light_states",
  device_command_status: "command_status",
} as const;
export type DeviceToolName = keyof typeof DEVICE_TOOL_INPUTS;
export type DeviceOperation = (typeof DEVICE_TOOL_OPERATIONS)[DeviceToolName];

export function isDeviceTool(name: string): name is DeviceToolName {
  return Object.hasOwn(DEVICE_TOOL_INPUTS, name);
}

export const DeviceToolResponse = z.discriminatedUnion("ok", [
  z.strictObject({ ok: z.literal(true), result: z.record(z.string(), z.unknown()) }),
  z.strictObject({ ok: z.literal(false), error: z.strictObject({
    code: z.enum(["invalid_input", "unauthenticated", "denied", "not_found", "conflict", "unavailable"]),
    message: z.string(),
  }) }),
]);
export type DeviceToolResult = z.infer<typeof DeviceToolResponse>;

export function deviceToolError(code: "invalid_input" | "unavailable"): DeviceToolResult {
  return { ok: false, error: { code, message: code === "invalid_input"
    ? "The device request is invalid." : "Device tools are unavailable. Please try again later." } };
}

export const DEVICE_TOOL_GUIDANCE = `
For home devices, lights, Home Assistant, Hue or WiZ, use the device tools.
1. Call device_list first. With no device, tell the user to open Ops, then Devices, then Link a device, and run the one-line command on the computer that stays home. Never ask for or show tokens.
2. With no Home Assistant installed, call device_install_package with package homeassistant.
3. Use device_ha_step for onboarding and for Hue, WiZ and group configuration flows. Hue needs the user to press the bridge button when the flow asks.
4. Use device_light and device_light_states to control and confirm. Always confirm by reading state after a change.
5. Commands are asynchronous: call device_command_status to poll a command. After a change, request device_light_states and read its completed command status to confirm the state. Never claim success before confirmation.
`;
