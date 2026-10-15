/**
 * Hermione (Staff Engineer): the chat review app's Apps Script connection. It does code reviews only.
 *
 * Every Chat message to the app comes to onMessage, which runs as the person who sent it. It reads only the event's
 * names, the sender's email and display name, and the message text with the app mention taken out. A message from a
 * sender in ALLOWED_SENDERS, in a space in ALLOWED_SPACES, whose text is exactly
 *   review https://github.com/<owner>/<repo>/pull/<n>
 * (the link may sit in <>), for a repo in ALLOWED_REPOS, becomes one row of the request Sheet, and the sender is told
 * it is queued. Anything else from them (a question, a task, extra words after the link) gets the fixed help text and
 * no row. A message from a sender not on the list, from a space not on the list (a direct message is a space like any
 * other, with no implicit allow) or while the Script Properties are not all set gets one fixed short reply and no row;
 * a bot sender, a non-message event or a malformed name gets none. Every rejection logs one fixed reason line
 * ('chat review: dropped (<reason>)') in the Apps Script executions log. No other text of a message is kept or logged
 * anywhere: the row holds the message's, thread's and space's names, the sender's email and display name,
 * the PR link rebuilt from what the grammar matched, the time and the status "queued".
 *
 * The row is appended through the Sheets API as the writer service account, whose JSON key is the Script Property
 * WRITER_KEY. The script signs a JWT bearer grant (RS256) with Utilities.computeRsaSha256Signature, trades it for an
 * access token for the spreadsheets scope, and keeps that token in the script cache for less than its lifetime. Neither
 * the key nor a token is ever logged or put in a reply. Under the script lock, the message's name is looked up in
 * column A first, so a message Chat delivers twice is one row. Cells are written RAW, so none is ever a formula.
 *
 * Script Properties: SHEET_ID, WRITER_KEY, ALLOWED_SENDERS, ALLOWED_SPACES and ALLOWED_REPOS (each list
 * comma-separated: emails, spaces/<id> names, owner/name repos).
 */

var APP_NAME = 'Hermione (Staff Engineer)';
var HELP = "I'm Hermione (Staff Engineer), and I do code reviews only. Send exactly: review" +
    ' https://github.com/OWNER/REPO/pull/NUMBER, for a repo I take. I don\'t answer questions or fix, build, run' +
    ' or deploy anything.';
var NOT_SET_UP = "I'm not set up yet.";
var SPACE_REFUSED = 'I only take review requests in approved conversations. Ask the owner to add this one.';
var SENDER_REFUSED = "You're not on the list for review requests yet. Ask the owner to add you.";
var QUEUED = 'Queued your review of ';
var BUSY = "I couldn't queue that just now. Send it again in a minute.";
var FULL = "My review queue is full right now. Ask the review desk's owner to clear it.";
var TOKEN_URL = 'https://oauth2.googleapis.com/token';
var SHEETS_ROOT = 'https://sheets.googleapis.com/v4/spreadsheets/';
var SHEETS_SCOPE = 'https://www.googleapis.com/auth/spreadsheets';
var JWT_BEARER = 'urn:ietf:params:oauth:grant-type:jwt-bearer';
var SHEET_TAB = 'requests';
var SHEET_MAX_ROWS = 5000;
var GRANT_SECONDS = 600;
var TOKEN_CACHE_KEY = 'writer-token';
var TOKEN_KEEP_MAX_SECONDS = 1800;
var LOCK_WAIT_MS = 10000;
var NAME_MAX = 100;
var PR_LINK = 'https://github\\.com/([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})/pull/([1-9][0-9]{0,9})';
var REQUEST_TEXT = new RegExp('^review +(?:<' + PR_LINK + '>|' + PR_LINK + ')$');
var MESSAGE_NAME = /^(spaces\/[A-Za-z0-9_-]{1,100})\/messages\/[A-Za-z0-9_.-]{1,200}$/;
var THREAD_NAME = /^spaces\/[A-Za-z0-9_-]{1,100}\/threads\/[A-Za-z0-9_.-]{1,200}$/;
var EMAIL = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$/;
var SHEET_ID = /^[A-Za-z0-9_-]{20,100}$/;

function onMessage(event) {
  var addOn = Boolean(event && event.chat);
  if (!addOn && (!event || event.type !== 'MESSAGE')) return {};
  var payload = addOn ? (event.chat.messagePayload || {}) : event;
  var message = payload.message || {};
  var space = payload.space || message.space || {};
  var user = (addOn ? event.chat.user : event.user) || {};
  if (user.type !== 'HUMAN') {
    console.log('chat review: dropped (not a human)');
    return {};
  }
  var name = text_(message.name);
  var spaceName = text_(space.name);
  var named = MESSAGE_NAME.exec(name);
  if (!named || named[1] !== spaceName) {
    console.log('chat review: dropped (bad message name)');
    return {};
  }
  var settings = settings_();
  if (!settings) {
    console.log('chat review: dropped (settings missing)');
    return reply_(addOn, NOT_SET_UP);
  }
  // Every conversation, a direct message included, must be listed: nothing here looks at the space's type.
  if (settings.spaces.indexOf(spaceName) < 0) {
    console.log('chat review: dropped (space not allowed)');
    return reply_(addOn, SPACE_REFUSED);
  }
  var email = text_(user.email).toLowerCase();
  if (!EMAIL.test(email) || settings.senders.indexOf(email) < 0) {
    console.log('chat review: dropped (sender not allowed)');
    return reply_(addOn, SENDER_REFUSED);
  }
  var thread = text_((message.thread || {}).name);
  if (!THREAD_NAME.test(thread) || thread.indexOf(spaceName + '/') !== 0) {
    console.log('chat review: dropped (bad thread)');
    return {};
  }
  var asked = typeof message.argumentText === 'string' ? message.argumentText : text_(message.text);
  var link = prLink_(asked, settings.repos);
  if (!link) return reply_(addOn, HELP);
  var row = [name, thread, spaceName, email, displayName_(user.displayName), link, new Date().toISOString(),
             'queued', ''];
  try {
    return reply_(addOn, appendOnce_(settings, row));
  } catch (err) {
    console.error('chat review: a request could not be queued');  // the error itself is never logged
    return reply_(addOn, BUSY);
  }
}

/** The PR link, rebuilt from what the grammar matched, when text is exactly a review request for an allowed repo. */
function prLink_(text, repos) {
  var match = REQUEST_TEXT.exec(text.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, ''));
  if (!match) return null;
  var owner = match[1] || match[4];
  var repo = match[2] || match[5];
  var number = match[3] || match[6];
  if (repos.indexOf((owner + '/' + repo).toLowerCase()) < 0) return null;
  return 'https://github.com/' + owner + '/' + repo + '/pull/' + number;
}

function reply_(addOn, text) {
  if (!addOn) return {text: text};
  return {hostAppDataAction: {chatDataAction: {createMessageAction: {message: {text: text}}}}};
}

function text_(value) {
  return typeof value === 'string' ? value : '';
}

function displayName_(value) {
  return text_(value).replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, NAME_MAX);
}

function list_(value, lower) {
  return text_(value).split(',').map(function (item) {
    item = item.trim();
    return lower ? item.toLowerCase() : item;
  }).filter(function (item) { return item.length > 0; });
}

/** The Script Properties, or null while any is missing. */
function settings_() {
  var props = PropertiesService.getScriptProperties();
  var settings = {
    sheet: text_(props.getProperty('SHEET_ID')),
    key: text_(props.getProperty('WRITER_KEY')),
    senders: list_(props.getProperty('ALLOWED_SENDERS'), true),
    spaces: list_(props.getProperty('ALLOWED_SPACES'), false),
    repos: list_(props.getProperty('ALLOWED_REPOS'), true)
  };
  if (!SHEET_ID.test(settings.sheet) || !settings.key || !settings.senders.length || !settings.spaces.length ||
      !settings.repos.length) {
    return null;
  }
  return settings;
}

/**
 * Append row unless a row for its message is already there or the Sheet holds SHEET_MAX_ROWS rows, under the script
 * lock. The reply the sender gets: queued, or the fixed full text.
 */
function appendOnce_(settings, row) {
  var lock = LockService.getScriptLock();
  lock.waitLock(LOCK_WAIT_MS);
  try {
    var token = writerToken_(settings.key);
    var base = SHEETS_ROOT + encodeURIComponent(settings.sheet) + '/values/';
    var names = sheets_(base + encodeURIComponent(SHEET_TAB + '!A2:A' + (SHEET_MAX_ROWS + 1)) +
                        '?majorDimension=COLUMNS', 'get', token, null);
    var column = (names.values && names.values[0]) || [];
    if (column.indexOf(row[0]) >= 0) return QUEUED + row[5];
    // The worker reads at most SHEET_MAX_ROWS rows, so a full Sheet takes no more until old rows are cleared.
    if (column.length >= SHEET_MAX_ROWS) return FULL;
    sheets_(base + encodeURIComponent(SHEET_TAB + '!A:I') + ':append?valueInputOption=RAW&insertDataOption=INSERT_ROWS',
            'post', token, {majorDimension: 'ROWS', values: [row]});
    return QUEUED + row[5];
  } finally {
    lock.releaseLock();
  }
}

function sheets_(url, method, token, body) {
  var options = {method: method, headers: {Authorization: 'Bearer ' + token}, muteHttpExceptions: true,
                 followRedirects: false};
  if (body) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(body);
  }
  var answer = UrlFetchApp.fetch(url, options);
  if (answer.getResponseCode() === 401) CacheService.getScriptCache().remove(TOKEN_CACHE_KEY);
  if (answer.getResponseCode() !== 200) throw new Error('Sheets answered HTTP ' + answer.getResponseCode());
  return JSON.parse(answer.getContentText());
}

/** An access token for the spreadsheets scope as the writer service account, from the script cache when one is held. */
function writerToken_(keyText) {
  var cache = CacheService.getScriptCache();
  var held = cache.get(TOKEN_CACHE_KEY);
  if (held) return held;
  var key = JSON.parse(keyText);
  if (key.type !== 'service_account' || typeof key.client_email !== 'string' || typeof key.private_key !== 'string') {
    throw new Error('WRITER_KEY is not a service account key');
  }
  var now = Math.floor(Date.now() / 1000);
  var input = b64url_(JSON.stringify({alg: 'RS256', typ: 'JWT'})) + '.' +
      b64url_(JSON.stringify({iss: key.client_email, scope: SHEETS_SCOPE, aud: TOKEN_URL, iat: now,
                              exp: now + GRANT_SECONDS}));
  var signature = Utilities.base64EncodeWebSafe(Utilities.computeRsaSha256Signature(input, key.private_key));
  var answer = UrlFetchApp.fetch(TOKEN_URL, {method: 'post', muteHttpExceptions: true, followRedirects: false,
      payload: {grant_type: JWT_BEARER, assertion: input + '.' + signature.replace(/=+$/, '')}});
  if (answer.getResponseCode() !== 200) throw new Error('the token endpoint answered HTTP ' + answer.getResponseCode());
  var data = JSON.parse(answer.getContentText());
  if (typeof data.access_token !== 'string' || typeof data.expires_in !== 'number') {
    throw new Error('the token endpoint gave no access token');
  }
  var keep = Math.min(data.expires_in - 300, TOKEN_KEEP_MAX_SECONDS);
  if (keep > 0) cache.put(TOKEN_CACHE_KEY, data.access_token, keep);
  return data.access_token;
}

function b64url_(text) {
  return Utilities.base64EncodeWebSafe(text, Utilities.Charset.UTF_8).replace(/=+$/, '');
}
