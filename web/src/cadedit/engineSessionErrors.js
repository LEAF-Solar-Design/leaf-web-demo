// The engine session's typed error kinds, in their own module so a PURE reader
// can have them. engineSession.js (the hook that owns the session) re-exports
// SESSION_ERROR unchanged, so every existing importer keeps its import path;
// web/src/lib/actionRegistry.js reads the constant from here instead, because
// it is React-free by contract and engineSession.js imports React.
//
// One value, one meaning, spelled once: a second copy of 'crashed' anywhere
// would let the Draw and Modify reason ladders drift apart from the store that
// sets the field.
export const SESSION_ERROR = Object.freeze({
  REFUSED: 'refused',     // the engine refused one edit; the document stands
  ENGINE: 'engine',       // the engine refused the document itself
  TRANSPORT: 'transport', // the boundary would not carry the message
  READ: 'read',           // the browser could not read the chosen file
  LIMIT: 'limit',         // the file is over the byte cap
  SAVE: 'save',           // the version write failed or was refused
  CRASHED: 'crashed',     // the worker died under us
})
