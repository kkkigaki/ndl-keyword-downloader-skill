on run argv
  set jsCode to item 1 of argv
  tell application "Google Chrome"
    return execute active tab of front window javascript jsCode
  end tell
end run
