ObjC.import('Foundation');

function shellQuote(value) {
  return "'" + value.replace(/'/g, "'\\''") + "'";
}

function run() {
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;
  const bundlePath = ObjC.unwrap($.NSBundle.mainBundle.bundlePath);
  const launcherPath = bundlePath + '/Contents/Resources/launch.sh';
  app.doShellScript(shellQuote(launcherPath) + ' >/dev/null 2>&1 &');
}
