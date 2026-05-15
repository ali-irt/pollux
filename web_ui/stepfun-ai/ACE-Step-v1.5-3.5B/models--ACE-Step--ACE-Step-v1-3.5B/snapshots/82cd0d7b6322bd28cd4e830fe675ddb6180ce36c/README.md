---
license: apache-2.0
tags:
- music
- text2music
- acestep
pipeline_tag: text-to-audio
language:
- en
- zh
- de
- fr
- es
- it
- pt
- pl
- tr
- ru
- cs
- nl
- ar
- ja
- hu
- ko
- hi
---

# ACE-Step: A Step Towards Music Generation Foundation Model

![ACE-Step Framework](https://github.com/ACE-Step/ACE-Step/raw/main/assets/ACE-Step_framework.png)

## Model Description

ACE-Step is a novel open-source foundation model for music generation that overcomes key limitations of existing approaches through a holistic architectural design. It integrates diffusion-based generation with Sana's Deep Compression AutoEncoder (DCAE) and a lightweight linear transformer, achieving state-of-the-art performance in generation speed, musical coherence, and controllability.

**Key Features:**
- 15× faster than LLM-based baselines (20s for 4-minute music on A100)
- Superior musical coherence across melody, harmony, and rhythm
- full-song generation, duration control and accepts natural language descriptions

## Uses

### Direct Use
ACE-Step can be used for:
- Generating original music from text descriptions
- Music remixing and style transfer
- edit song lyrics

### Downstream Use
The model serves as a foundation for:
- Voice cloning applications
- Specialized music generation (rap, jazz, etc.)
- Music production tools
- Creative AI assistants

### Out-of-Scope Use
The model should not be used for:
- Generating copyrighted content without permission
- Creating harmful or offensive content
- Misrepresenting AI-generated music as human-created

## How to Get Started

see: https://github.com/ace-step/ACE-Step

## Hardware Performance

| Device        | 27 Steps | 60 Steps |
|---------------|----------|----------|
| NVIDIA A100   | 27.27x   | 12.27x   |
| RTX 4090      | 34.48x   | 15.63x   |
| RTX 3090      | 12.76x   | 6.48x    |
| M2 Max        | 2.27x    | 1.03x    |

*RTF (Real-Time Factor) shown - higher values indicate faster generation*


## Limitations

- Performance varies by language (top 10 languages perform best)
- Longer generations (>5 minutes) may lose structural coherence
- Rare instruments may not render perfectly
- Output Inconsistency: Highly sensitive to random seeds and input duration, leading to varied "gacha-style" results.
- Style-specific Weaknesses: Underperforms on certain genres (e.g. Chinese rap/zh_rap) Limited style adherence and musicality ceiling
- Continuity Artifacts: Unnatural transitions in repainting/extend operations
- Vocal Quality: Coarse vocal synthesis lacking nuance
- Control Granularity: Needs finer-grained musical parameter control

## Ethical Considerations

Users should:
- Verify originality of generated works
- Disclose AI involvement
- Respect cultural elements and copyrights
- Avoid harmful content generation


## Model Details

**Developed by:** ACE Studio and StepFun  
**Model type:** Diffusion-based music generation with transformer conditioning  
**License:** Apache 2.0  
**Resources:**  
- [Project Page](https://ace-step.github.io/)
- [Demo Space](https://huggingface.co/spaces/ACE-Step/ACE-Step)
- [GitHub Repository](https://github.com/ACE-Step/ACE-Step)


## Citation

```bibtex
@misc{gong2025acestep,
  title={ACE-Step: A Step Towards Music Generation Foundation Model},
  author={Junmin Gong, Wenxiao Zhao, Sen Wang, Shengyuan Xu, Jing Guo}, 
  howpublished={\url{https://github.com/ace-step/ACE-Step}},
  year={2025},
  note={GitHub repository}
}
```

## Acknowledgements
This project is co-led by ACE Studio and StepFun.