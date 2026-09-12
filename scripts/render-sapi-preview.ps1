param(
    [Parameter(Mandatory = $true)]
    [string]$StoryJson,

    [Parameter(Mandatory = $true)]
    [string]$OutputWav,

    [string]$Voice = 'Microsoft Huihui Desktop'
)

$ErrorActionPreference = 'Stop'

$storyPath = (Resolve-Path -LiteralPath $StoryJson).Path
$outputPath = [System.IO.Path]::GetFullPath($OutputWav)
$outputParent = [System.IO.Path]::GetDirectoryName($outputPath)
if (-not [System.IO.Directory]::Exists($outputParent)) {
    [System.IO.Directory]::CreateDirectory($outputParent) | Out-Null
}

$story = Get-Content -Raw -LiteralPath $storyPath | ConvertFrom-Json

Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
    24000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono
)

try {
    try {
        $synth.SelectVoice($Voice)
    }
    catch {
        throw "SAPI voice '$Voice' is not available in this process. Use an installed SAPI voice or render with the local Higgs TTS service."
    }
    $synth.SetOutputToWaveFile($outputPath, $format)

    $parts = [System.Collections.Generic.List[string]]::new()
    $parts.Add('<speak version="1.0" xml:lang="zh-CN">')
    $parts.Add('<prosody rate="-18%" volume="medium">')
    $title = [System.Security.SecurityElement]::Escape([string]$story.title)
    $parts.Add("<p><s>$title。</s></p><break time=`"900ms`"/>")

    foreach ($page in $story.pages) {
        $narration = [System.Security.SecurityElement]::Escape([string]$page.narration)
        $parts.Add("<p><s>$narration</s></p>")
        foreach ($line in $page.dialogue) {
            $speaker = [System.Security.SecurityElement]::Escape([string]$line.speaker)
            $text = [System.Security.SecurityElement]::Escape([string]$line.text)
            $parts.Add("<break time=`"350ms`"/><s>$speaker 说，$text</s>")
        }
        if ($page.interaction) {
            $interaction = [System.Security.SecurityElement]::Escape([string]$page.interaction)
            $parts.Add("<break time=`"450ms`"/><prosody rate=`"-25%`"><s>$interaction</s></prosody><break time=`"2400ms`"/>")
        }
        else {
            $parts.Add('<break time="950ms"/>')
        }
    }

    $parts.Add('</prosody></speak>')
    $ssml = $parts -join ''
    $synth.SpeakSsml($ssml)
}
finally {
    $synth.Dispose()
}

$file = Get-Item -LiteralPath $outputPath
if ($file.Length -lt 1024) {
    throw "Generated WAV is unexpectedly small: $($file.Length) bytes"
}

[pscustomobject]@{
    Path = $file.FullName
    Bytes = $file.Length
    Voice = $Voice
    SampleRate = 24000
    Channels = 1
}
