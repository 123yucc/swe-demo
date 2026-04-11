import torch
from torch import Tensor
import torch.nn as nn
# from .._internally_replaced_utils import load_state_dict_from_url
from typing import Type, Any, Callable, Union, List
# from MODEL.cbam import CBAM, CBAM_P, CBAM_Q
from MODEL.cbam import CBAM

# 定义了一个名为PaB的注意力模块
# 它封装了CBAM注意力机制。该模块接收特征图，输出空间注意力矩阵和通道注意力矩阵


class PaB(nn.Module):
    def __init__(self, planes: int) -> None:
        super(PaB, self).__init__()
        self.att_block = CBAM(planes * 4, 16)

    def forward(self, x):
        spa_matrix, cha_matrix = self.att_block(x)
        return spa_matrix, cha_matrix
