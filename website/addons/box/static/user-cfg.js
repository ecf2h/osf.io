var BoxUserConfig = require('./boxUserConfig.js');

// Endpoint for box user settings
var url = '/api/v1/settings/box/accounts/';
// Start up the Box Config manager
new BoxUserConfig('#boxAddonScope', url);
