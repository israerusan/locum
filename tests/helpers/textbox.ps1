# A window with one native text box (Win32 EDIT control) for the real-desktop focus tests.
#   powershell -NoProfile -ExecutionPolicy Bypass -File textbox.ps1 -Title T -Out FILE -X 100 -Y 100 [-Topmost]
# Writes the box's text to FILE every 50 ms, so a test can read what reached it. Closes after 120 s.
param([string]$Title, [string]$Out, [int]$X = 100, [int]$Y = 100, [switch]$Topmost)
Add-Type -AssemblyName System.Windows.Forms
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point($X, $Y)
$form.Size = New-Object System.Drawing.Size(420, 240)
$form.TopMost = [bool]$Topmost
$box = New-Object System.Windows.Forms.TextBox
$box.Multiline = $true
$box.AcceptsReturn = $true
$box.AcceptsTab = $true
$box.Dock = 'Fill'
$box.AccessibleName = 'Text box'
$form.Controls.Add($box)
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 50
$timer.Add_Tick({ [System.IO.File]::WriteAllText($Out + '.tmp', $box.Text); Move-Item -Force ($Out + '.tmp') $Out })
$timer.Start()
$stop = New-Object System.Windows.Forms.Timer
$stop.Interval = 120000
$stop.Add_Tick({ $form.Close() })
$stop.Start()
$form.Add_Shown({ $box.Focus() | Out-Null })
[System.Windows.Forms.Application]::Run($form)
