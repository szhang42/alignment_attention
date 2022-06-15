
We adapt our code from huggingface. For details, please refer to the [original repository](https://github.com/huggingface/transformers).

Our code works for ALBERT. The codebase is compatible for finetuning pretrained ALBERT model on GLUE and 
SQUAD datasets. We provide an example bash file under examples folder to finetune ALBERT. The flag
adver_type controls the alignment method was used. 


## Citation

```bibtex
@article{zhang2021alignment,
  title={Alignment Attention by Matching Key and Query Distributions},
  author={Zhang, Shujian and Fan, Xinjie and Zheng, Huangjie and Tanwisuth, Korawat and Zhou, Mingyuan},
  journal={arXiv preprint arXiv:2110.12567},
  year={2021}
}
```