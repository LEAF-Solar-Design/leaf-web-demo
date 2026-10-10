export function isCompositionKey(event) {
  return event?.isComposing === true
    || event?.nativeEvent?.isComposing === true
    || event?.keyCode === 229
    || event?.nativeEvent?.keyCode === 229
}
