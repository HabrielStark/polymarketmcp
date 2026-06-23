# Hermes-PM Media

This folder contains the rendered portfolio/demo videos and the HyperFrames
source projects used to build them.

## Rendered videos

| File | Duration | Resolution | FPS | Purpose |
|------|----------|------------|-----|---------|
| [`videos/hermes-pm-motion-demo.mp4`](videos/hermes-pm-motion-demo.mp4) | 42s | 1920x1080 | 60 | Polished motion-graphics product overview. |
| [`videos/hermes-pm-terminal-walkthrough.mp4`](videos/hermes-pm-terminal-walkthrough.mp4) | 58s | 1920x1080 | 60 | Screen-recording-style terminal and browser walkthrough. |

GitHub's normal blob viewer may refuse to preview MP4 files and show "Sorry, we
can't show files that are this big right now." Use the direct playable URLs:

- Motion demo:
  `https://cdn.jsdelivr.net/gh/HabrielStark/polymarketmcp@main/media/videos/hermes-pm-motion-demo.mp4`
- Terminal walkthrough:
  `https://cdn.jsdelivr.net/gh/HabrielStark/polymarketmcp@main/media/videos/hermes-pm-terminal-walkthrough.mp4`

The README uses animated GIF previews from `assets/brand` so the demos are
visibly video-like on the repository landing page.

## Source projects

| Project | Description |
|---------|-------------|
| [`hyperframes/hermes-pm-motion-demo`](hyperframes/hermes-pm-motion-demo) | Five-scene motion-graphics composition. |
| [`hyperframes/hermes-pm-terminal-demo`](hyperframes/hermes-pm-terminal-demo) | Five-scene manual walkthrough composition. |

## Verification commands

Run from each HyperFrames project directory:

```powershell
npm run check
```

Render commands used for the committed videos:

```powershell
cd media\hyperframes\hermes-pm-motion-demo
npx --yes hyperframes@0.7.3 render --output ..\..\videos\hermes-pm-motion-demo.mp4 --fps 60 --quality high --workers=1 --strict-all

cd ..\hermes-pm-terminal-demo
npx --yes hyperframes@0.7.3 render --output ..\..\videos\hermes-pm-terminal-walkthrough.mp4 --fps 60 --quality high --workers=1 --strict-all
```

Video metadata verification:

```powershell
ffprobe -v error -show_entries format=duration,size,format_name -show_entries stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames -of json videos\hermes-pm-motion-demo.mp4
ffprobe -v error -show_entries format=duration,size,format_name -show_entries stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames -of json videos\hermes-pm-terminal-walkthrough.mp4
```
