/**
 * Hermione (Staff Engineer): the chat review app's Apps Script connection. It does nothing.
 *
 * Chat calls these handlers, the Workspace add-on's Chat triggers, for each message, each time the app is added to or
 * removed from a conversation, and for each app command. Every one answers with null, the empty response Google
 * documents for an app that replies later through the Chat API: it reads nothing of the event, keeps and logs nothing,
 * and calls no Google service, and the manifest asks for no scope, so no one who writes to the app is ever asked to
 * authorize anything.
 *
 * The reviews happen on the owner's Mac. Its chat-review job reads new messages in the conversations the app is a
 * member of with the owner's own read-only authorization, and replies in the thread as the app.
 */

function onMessage(event) {
  return null;
}

function onAddedToSpace(event) {
  return null;
}

function onRemovedFromSpace(event) {
  return null;
}

function onAppCommand(event) {
  return null;
}
