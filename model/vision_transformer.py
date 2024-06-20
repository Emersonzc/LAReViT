import random

import torch
import torch.nn as nn
import math
import torch.nn.functional as F
from itertools import repeat
import collections.abc as container_abcs
from typing import Tuple, Union
from collections import OrderedDict

class ChannelAttention(nn.Module):
    def __init__(self, in_planes, ratio=16):
        super(ChannelAttention, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool1d(1)
        self.max_pool = nn.AdaptiveMaxPool1d(1)
           
        self.fc = nn.Sequential(nn.Conv2d(in_planes, in_planes // 16, 1, bias=False),
                               nn.ReLU(),
                               nn.Conv2d(in_planes // 16, in_planes, 1, bias=False))
        self.sigmoid = nn.Sigmoid()
    #torch.Size([64, 256, 72, 36])
    #torch[64,254,768]
    def forward(self, x):
        #torch.Size([64, 256, 1, 1])
        avg_out = self.fc(self.avg_pool(x).unsqueeze(3))
        #torch.Size([64, 256, 1, 1])
        max_out = self.fc(self.max_pool(x).unsqueeze(3))
        out = avg_out + max_out
        #torch.Size([64, 256, 1, 1])
        return self.sigmoid(out.squeeze(3))

class SpatialAttention(nn.Module):
    def __init__(self, kernel_size=7):
        super(SpatialAttention, self).__init__()

        self.conv1 = nn.Conv2d(2, 1, kernel_size, padding=kernel_size//2, bias=False)
        self.sigmoid = nn.Sigmoid()
    #torch.Size([64, 256, 72, 36])
    def forward(self, x):
        #torch.Size([64, 1, 72, 36])
        avg_out = torch.mean(x, dim=1, keepdim=True)
        #torch.Size([64, 1, 72, 36])
        max_out, _ = torch.max(x, dim=1, keepdim=True)
        #torch.Size([64, 2, 72, 36])
        x = torch.cat([avg_out, max_out], dim=1)
        
        x = self.conv1(x.unsqueeze(3)).squeeze(3)
        
        return self.sigmoid(x)


class QuickGLUE(nn.Module):
    """
    Applies GELU approximation that is fast but somewhat inaccurate. See: https://github.com/hendrycks/GELUs
    """

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x * torch.sigmoid(1.702 * x)
   

class HXCMM(nn.Module):
    def __init__(self, edim=768):
        super(HXCMM, self).__init__()
        self.norm1 = nn.LayerNorm(768)
        self.norm2 = nn.LayerNorm(768)
        self.norm3 = nn.LayerNorm(768)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(edim, edim * 4)),
            ("gelu", QuickGLUE()),
            ("c_proj", nn.Linear(edim * 4, edim))
        ]))
        self.attn = nn.MultiheadAttention(embed_dim=768,num_heads=12,batch_first = True)
        #torch.Size([64, 256, 72, 36])
    def forward(self, x, x_ori):

        x = x + self.attn(self.norm1(x_ori),self.norm1(x_ori),self.norm2(x),need_weights=False)[0]
        x = x + self.mlp(self.norm3(x))
        
        return x

class HXCM(nn.Module):
    def __init__(self, edim=768):
        super(HXCM, self).__init__()
        self.norm1 = nn.LayerNorm(768)
        self.norm2 = nn.LayerNorm(768)


        self.attn = nn.MultiheadAttention(embed_dim=768,num_heads=768//4,batch_first = True)
        #torch.Size([64, 256, 72, 36])
    def forward(self, x, x_ori):

        x = x + self.attn(x_ori,x_ori,x,need_weights=False)[0]

        return x
    
def _ntuple(n):
    def parse(x):
        if isinstance(x, container_abcs.Iterable):
            return x
        return tuple(repeat(x, n))
    return parse

to_2tuple = _ntuple(2)

def weights_init_classifier(m):
    classname = m.__class__.__name__
    if classname.find('Linear') != -1:
        nn.init.normal_(m.weight, std=0.001)
        if m.bias:
            nn.init.constant_(m.bias, 0.0)

def drop_path(x, drop_prob: float = 0., training: bool = False):
    """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).
    This is the same as the DropConnect impl I created for EfficientNet, etc networks, however,
    the original name is misleading as 'Drop Connect' is a different form of dropout in a separate paper...
    See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956 ... I've opted for
    changing the layer and argument names to 'drop path' rather than mix DropConnect as a layer name and use
    'survival rate' as the argument.
    """
    if drop_prob == 0. or not training:
        return x
    keep_prob = 1 - drop_prob
    shape = (x.shape[0],) + (1,) * (x.ndim - 1)  # work with diff dim tensors, not just 2D ConvNets
    random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
    random_tensor.floor_()  # binarize
    output = x.div(keep_prob) * random_tensor
    return output

class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks).
    """
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)

def resize_pos_embed(posemb, posemb_new, hight, width):
    # Rescale the grid of position embeddings when loading from state_dict. Adapted from
    # https://github.com/google-research/vision_transformer/blob/00883dd691c63a6830751563748663526e811cee/vit_jax/checkpoint.py#L224
    ntok_new = posemb_new.shape[1]

    posemb_token, posemb_grid = posemb[:, :1], posemb[0, 1:]
    ntok_new -= 1

    gs_old = int(math.sqrt(len(posemb_grid)))
    print('Resized position embedding from size:{} to size: {} with height:{} width: {}'.format(posemb.shape, posemb_new.shape, hight, width))
    posemb_grid = posemb_grid.reshape(1, gs_old, gs_old, -1).permute(0, 3, 1, 2)
    posemb_grid = F.interpolate(posemb_grid, size=(hight, width), mode='bilinear')
    posemb_grid = posemb_grid.permute(0, 2, 3, 1).reshape(1, hight * width, -1)
    posemb = torch.cat([posemb_token, posemb_grid], dim=1)
    return posemb

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x

class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x,x_ori):
        if x.equal(x_ori):
            B, N, C = x.shape
            qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

            attn = (q @ k.transpose(-2, -1)) * self.scale
            attn = attn.softmax(dim=-1)
            attn = self.attn_drop(attn)

            x = (attn @ v).transpose(1, 2).reshape(B, N, C) #[128, 211, 768]
            x = self.proj(x)
            x = self.proj_drop(x)
            return x
        else:
            B, N, C = x.shape
            qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            qkv_ori = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            q, k, v = 0.5*qkv_ori[0]+0.5*qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

            attn = (q @ k.transpose(-2, -1)) * self.scale
            attn = attn.softmax(dim=-1)
            attn = self.attn_drop(attn)

            x = (attn @ v).transpose(1, 2).reshape(B, N, C) #[128, 211, 768]
            x = self.proj(x)
            x = self.proj_drop(x)
            return x

class Block(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x,x_ori):
        x = x + self.drop_path(self.attn(self.norm1(x),self.norm1(x_ori)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x

class PatchEmbed_overlap(nn.Module):
    """ Image to Patch Embedding with overlapping patches"""
    def __init__(self, img_size=224, patch_size=16, stride_size=20, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        stride_size_tuple = to_2tuple(stride_size)
        self.num_x = (img_size[1] - patch_size[1]) // stride_size_tuple[1] + 1
        self.num_y = (img_size[0] - patch_size[0]) // stride_size_tuple[0] + 1
        print('using stride: {}, and patch number is num_y{} * num_x{}'.format(stride_size, self.num_y, self.num_x))
        num_patches = self.num_x * self.num_y
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches
        #patch_size=[16,16], stride_size=[12,12]
        self.proj  =  nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride_size)
        #self.proj  =  nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride_size,dilation=3,padding=12)
        #self.proj  =  nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride_size,dilation=2,padding=6)
       
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.InstanceNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def forward(self, x):
        B, C, H, W = x.shape  #batch_size , channels , height ,width
   
        # FIXME look at relaxing size constraints
        assert H == self.img_size[0] and W == self.img_size[1], \
            f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x)
        #x = x.flatten(2).transpose(1, 2)
        return x


class DLK(nn.Module):
    """ Image to Patch Embedding with overlapping patches"""
    def __init__(self, img_size=224, patch_size=16, stride_size=20, in_chans=3, embed_dim=768):
        super(DLK, self).__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        stride_size_tuple = to_2tuple(stride_size)
        self.num_x = (img_size[1] - patch_size[1]) // stride_size_tuple[1] + 1
        self.num_y = (img_size[0] - patch_size[0]) // stride_size_tuple[0] + 1
        print('using stride: {}, and patch number is num_y{} * num_x{}'.format(stride_size, self.num_y, self.num_x))
        num_patches = self.num_x * self.num_y
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches
        #patch_size=[16,16], stride_size=[12,12]
        self.proj  =  nn.Conv2d(embed_dim, embed_dim, kernel_size=(3,3), stride=1,padding=(1,1))
        self.dc = nn.Conv2d(2*embed_dim, embed_dim, kernel_size=(1,1), stride=1)
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.InstanceNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def forward(self, x):
        B, C, H, W = x.shape  #batch_size , channels , height ,width
        xtemp=x

        x = self.proj(x)
        #x = x.flatten(2).transpose(1, 2)
        x=torch.cat([xtemp,x],dim=1)
        x = self.dc(x)
        return x


class ViT(nn.Module):
    def __init__(self, img_size=224, patch_size=16, stride_size=16, in_chans=3, num_classes=1000, embed_dim=768, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=True, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., norm_layer = nn.LayerNorm):
        super(ViT, self).__init__()
        self.num_classes = num_classes

        self.patch_embed = PatchEmbed_overlap(img_size=img_size, patch_size=patch_size, stride_size=stride_size, in_chans=in_chans,embed_dim=embed_dim)

        num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))

        self.pos_drop = nn.Dropout(p=drop_rate)
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule

        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.norm = norm_layer(embed_dim)
        self.DLK =DLK(img_size=img_size, patch_size=patch_size, stride_size=stride_size, in_chans=in_chans,embed_dim=embed_dim)

        trunc_normal_(self.cls_token, std=.02)
        trunc_normal_(self.pos_embed, std=.02)
        self.apply(self._init_weights)
        layer_init_values=1e-5

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)

        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward_features(self, x):
        B = x.shape[0]
        #torch.Size([16, 3, 288, 144])
        # x = self.patch_embed(x)
        # x = x.flatten(2).transpose(1, 2)
        #torch.Size([16, 253, 768])
        #x = self.DLK(x)+x
        #x = x.flatten(2).transpose(1, 2)
        #index1 = torch.LongTensor(random.sample(range(50), 50))
        index1 = torch.LongTensor(random.sample(range(80), 80))
        index1, _ = torch.sort(index1)
        index1 = index1.cuda().detach()

        #index2 = torch.LongTensor(random.sample(range(60, 150), 90))
        index2 = torch.LongTensor(random.sample(range(80, 205), 125))
        index2, _ = torch.sort(index2)
        index2 = index2.cuda().detach()

        #index3 = torch.LongTensor(random.sample(range(150, 240), 90))
        index3 = torch.LongTensor(random.sample(range(205, 330), 125))
        index3, _ = torch.sort(index3)
        index3 = index3.cuda().detach()

        pos_token = self.pos_embed[:, :1]

        pos_embed = self.pos_embed[:, 1:]
        pos_embed1 = torch.cat([pos_token, pos_embed[:, index1]], dim=1)
        pos_embed2 = torch.cat([pos_token, pos_embed[:, index2]], dim=1)
        pos_embed3 = torch.cat([pos_token, pos_embed[:, index3]], dim=1)

        x_1 = torch.index_select(x, 1, index1)
        x_2 = torch.index_select(x, 1, index2)
        x_3 = torch.index_select(x, 1, index3)

        cls_tokens = self.cls_token.expand(B, -1, -1)

        x = torch.cat((cls_tokens, x), dim=1) + self.pos_embed
        x_1 = torch.cat((cls_tokens, x_1), dim=1) + pos_embed1
        x_2 = torch.cat((cls_tokens, x_2), dim=1) + pos_embed2
        x_3 = torch.cat((cls_tokens, x_3), dim=1) + pos_embed3


        x = self.pos_drop(x)

        # for blk in self.blocks:
        #     x =   blk(x)
        #     x_1 = blk(x_1)
        #     x_2 = blk(x_2)
        #     x_3 = blk(x_3)
        #xtemp0_0=x
        x_ori0=x
        x_ori01=x_1
        x_ori02=x_2
        x_ori03=x_3
        x   = self.blocks[0](x  ,x  )        
        x_1 = self.blocks[0](x_1,x_1)
        x_2 = self.blocks[0](x_2,x_2)
        x_3 = self.blocks[0](x_3,x_3)      
        x_ori1=x
        x_ori11=x_1
        x_ori12=x_2
        x_ori13=x_3
        x   = self.blocks[1](x  ,x)
        #x   = self.blocks[1](x  ,x_ori0  )  
        x_1 = self.blocks[1](x_1,x_ori01)
        x_2 = self.blocks[1](x_2,x_ori02)
        x_3 = self.blocks[1](x_3,x_ori03)


        x_ori2=x
        x_ori21=x_1
        x_ori22=x_2
        x_ori23=x_3
        x   = self.blocks[2](x  ,x)               
        #x =   self.blocks[2](x  ,x_ori1  )   
        x_1 = self.blocks[2](x_1,x_ori11)
        x_2 = self.blocks[2](x_2,x_ori12)
        x_3 = self.blocks[2](x_3,x_ori13)


        x_ori3=x
        x_ori31=x_1
        x_ori32=x_2
        x_ori33=x_3
        x   = self.blocks[3](x  ,x)                   
        #x =   self.blocks[3](x  ,x_ori2  )
        x_1 = self.blocks[3](x_1,x_ori21)
        x_2 = self.blocks[3](x_2,x_ori22)
        x_3 = self.blocks[3](x_3,x_ori23)


        x_ori4=x
        x_ori41=x_1
        x_ori42=x_2
        x_ori43=x_3
        x   = self.blocks[4](x  ,x)                    
        #x =   self.blocks[4](x  ,x_ori3  )
        x_1 = self.blocks[4](x_1,x_ori31)
        x_2 = self.blocks[4](x_2,x_ori32)
        x_3 = self.blocks[4](x_3,x_ori33)
        

        x_ori5=x
        x_ori51=x_1
        x_ori52=x_2
        x_ori53=x_3
        x   = self.blocks[5](x  ,x)                          
        #x =   self.blocks[5](x  ,x_ori4  )
        x_1 = self.blocks[5](x_1,x_ori41)
        x_2 = self.blocks[5](x_2,x_ori42)
        x_3 = self.blocks[5](x_3,x_ori43)
        

        x_ori6=x
        x_ori61=x_1
        x_ori62=x_2
        x_ori63=x_3
        x   = self.blocks[6](x  ,x)                      
        #x =   self.blocks[6](x  ,x_ori5  )
        x_1 = self.blocks[6](x_1,x_ori51)
        x_2 = self.blocks[6](x_2,x_ori52)
        x_3 = self.blocks[6](x_3,x_ori53)

        x_ori7=x
        x_ori71=x_1
        x_ori72=x_2
        x_ori73=x_3
        x   = self.blocks[7](x  ,x)                                      
        #x =   self.blocks[7](x  ,x_ori6  )
        x_1 = self.blocks[7](x_1,x_ori61)
        x_2 = self.blocks[7](x_2,x_ori62)
        x_3 = self.blocks[7](x_3,x_ori63)

        x_ori8=x
        x_ori81=x_1
        x_ori82=x_2
        x_ori83=x_3
        x   = self.blocks[8](x  ,x)                           
        #x =   self.blocks[8](x  ,x_ori7  )
        x_1 = self.blocks[8](x_1,x_ori71)
        x_2 = self.blocks[8](x_2,x_ori72)
        x_3 = self.blocks[8](x_3,x_ori73)


        x_ori9=x
        x_ori91=x_1
        x_ori92=x_2
        x_ori93=x_3
        x   = self.blocks[9](x  ,x)                    
        #x =   self.blocks[9](x  ,x_ori8  )
        x_1 = self.blocks[9](x_1,x_ori81)
        x_2 = self.blocks[9](x_2,x_ori82)
        x_3 = self.blocks[9](x_3,x_ori83)

   
        xtemp1=x  
        x =   self.blocks[10](x  ,x  )
        x_1 = self.blocks[10](x_1,x_1)
        x_2 = self.blocks[10](x_2,x_2)
        x_3 = self.blocks[10](x_3,x_3)
        
        xtemp2=x        
        x =   self.blocks[11](x  ,x  )
        x_1 = self.blocks[11](x_1,x_1)
        x_2 = self.blocks[11](x_2,x_2)
        x_3 = self.blocks[11](x_3,x_3)
        #xtemp=x_1[:, 0]
        return x[:, 0],x_1[:, 0],x_2[:, 0],x_3[:, 0],xtemp1[:, 0]

    def forward(self,x):
        x = self.forward_features(x)
        return x

    def load_param(self, model_path):
        param_dict = torch.load(model_path, map_location='cpu')
        if 'model' in param_dict:
            param_dict = param_dict['model']
        if 'state_dict' in param_dict:
            param_dict = param_dict['state_dict']
        for k, v in param_dict.items():
            if 'head' in k or 'dist' in k:
                continue
            if 'patch_embed.proj.weight' in k and len(v.shape) < 4:
                # For old models that I trained prior to conv based patchification
                O, I, H, W = self.patch_embed.proj.weight.shape
                v = v.reshape(O, -1, H, W)
            elif k == 'pos_embed' and v.shape != self.pos_embed.shape:
                # To resize pos embedding when using model at different size from pretrained weights
                if 'distilled' in model_path:
                    print('distill need to choose right cls token in the pth')
                    v = torch.cat([v[:, 0:1], v[:, 2:]], dim=1)
                v = resize_pos_embed(v, self.pos_embed, self.patch_embed.num_y, self.patch_embed.num_x)
            try:
                self.state_dict()[k].copy_(v)
            except:
                print('===========================ERROR=========================')
                print('shape do not match in k :{}: param_dict{} vs self.state_dict(){}'.format(k, v.shape, self.state_dict()[k].shape))

def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    r"""
    Examples:
        >>> w = torch.empty(3, 5)
        >>> nn.init.trunc_normal_(w)
    """
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)

def _no_grad_trunc_normal_(tensor, mean, std, a, b):   #标准化
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        print("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor