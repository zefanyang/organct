#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @Time  : 7/2/2021 9:49 AM
# @Author: yzf
"""Encoder, DeepSupervision, Decoder-Head and RFP-Head"""

####### Need test #########
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchsummary import summary
from models.unet import Encoder, Decoder, DoubleConv
from models.unet_nine_layers.unet_l9_deep_sup import DeepSup

from models.utils_graphical_model import UAG_RNN_4Neigh, UAG_RNN_8Neigh

class UNetL9DeepSupRFP_MultiHead(nn.Module):
    def __init__(self, in_ch, out_ch, interpolate=True, init_ch=16, conv_layer_order='cbr', num_neigh='four'):
        super(UNetL9DeepSupRFP_MultiHead, self).__init__()

        self.no_class = out_ch

        ## Encoder
        self.encoders = nn.ModuleList([
            Encoder(in_ch, init_ch, is_max_pool=False, conv_layer_order=conv_layer_order),
            Encoder(init_ch, 2 * init_ch, conv_layer_order=conv_layer_order),
            Encoder(2 * init_ch, 4 * init_ch, conv_layer_order=conv_layer_order),
            Encoder(4 * init_ch, 8 * init_ch, conv_layer_order=conv_layer_order),
            Encoder(8 * init_ch, 16 * init_ch, conv_layer_order=conv_layer_order)
        ])

        ## Decoder-Head
        self.decoders = nn.ModuleList([
            Decoder(8 * init_ch + 16 * init_ch, 8 * init_ch, interpolate, conv_layer_order=conv_layer_order),
            Decoder(4 * init_ch + 8 * init_ch, 4 * init_ch, interpolate, conv_layer_order=conv_layer_order),
            Decoder(2 * init_ch + 4 * init_ch, 2 * init_ch, interpolate, conv_layer_order=conv_layer_order),
            Decoder(init_ch + 2 * init_ch, init_ch, interpolate, conv_layer_order=conv_layer_order)
        ])

        # Deep Supervision
        self.deep_sup4 = DeepSup(8 * init_ch, out_ch=self.no_class, scale_factor=8)
        self.deep_sup3 = DeepSup(4 * init_ch, out_ch=self.no_class, scale_factor=4)
        self.deep_sup2 = DeepSup(2 * init_ch, out_ch=self.no_class, scale_factor=2)
        self.deep_sup1 = nn.Conv3d(init_ch, self.no_class, kernel_size=1)

        self.final_conv = nn.Sequential(nn.Dropout3d(0.1, False),
                                        nn.Conv3d(self.no_class * 4, self.no_class, 1))

        ## RFP-Head
        # Decrease the input channels by factor of two.
        # Comparing to the 64-channel version, see substantial performance gain
        trans_ch = 16 * init_ch // 2

        self.adapt = nn.Sequential(
            nn.Conv3d(16*init_ch, trans_ch, 3, padding=1, bias=False),
            nn.BatchNorm3d(trans_ch),
            nn.ReLU(),
        )

        # Add another convolutional unit.
        # But, see performance drop
            # nn.Conv3d(trans_ch, trans_ch, 3, padding=1, bias=False),
            # nn.BatchNorm3d(trans_ch),
            # nn.ReLU()
        # )

        self.rfp = RFP_UAGs(in_ch=trans_ch, num_neigh=num_neigh)
        self.rfp_fnl_conv = nn.Sequential(
            nn.Conv3d(trans_ch, trans_ch, 3, padding=1, bias=False),
            nn.BatchNorm3d(trans_ch),
            nn.ReLU(),
            nn.Conv3d(trans_ch, self.no_class, 1)
        )

        # Out conv
        self.comb_fnl_conv = nn.Conv3d(self.no_class * 2, self.no_class, 1)

    def forward(self, x):
        # Encoder
        encoders_features = []
        enc1 = self.encoders[0](x)
        enc2 = self.encoders[1](enc1)
        enc3 = self.encoders[2](enc2)
        enc4 = self.encoders[3](enc3)
        mid = self.encoders[4](enc4)
        encoders_features = [enc4, enc3, enc2, enc1]

        # Decoder-Head
        dec4 = self.decoders[0](enc4, mid)
        dec3 = self.decoders[1](enc3, dec4)
        dec2 = self.decoders[2](enc2, dec3)
        dec1 = self.decoders[3](enc1, dec2)

        dsup4 = self.deep_sup4(dec4)
        dsup3 = self.deep_sup3(dec3)
        dsup2 = self.deep_sup2(dec2)
        dsup1 = self.deep_sup1(dec1)

        seg_score = self.final_conv(torch.cat((dsup4, dsup3, dsup2, dsup1), dim=1))

        # RFP-Head
        mid_adapt = self.adapt(mid)
        ehn_mid = self.rfp(mid_adapt)
        rfp_seg_score = self.rfp_fnl_conv(ehn_mid)
        rfp_seg_score = F.upsample(rfp_seg_score, scale_factor=16, mode='trilinear', align_corners=True)

        # comb_seg_score = seg_score + rfp_seg_score

        # This follows the deep supervision routine.
        # See slightly performance gain.
        comb_seg_score = self.comb_fnl_conv(torch.cat((seg_score, rfp_seg_score), 1))

        return seg_score, comb_seg_score

class RFP_UAGs(nn.Module):
    def __init__(self, in_ch, num_neigh='four'):
        super().__init__()
        self.dag_list = None
        if num_neigh == 'four':
            self.dag_list = nn.ModuleList([UAG_RNN_4Neigh(in_ch) for _ in range(64//16)])  # hard-coding `64//16`
        elif num_neigh == 'eight':
            self.dag_list = nn.ModuleList([UAG_RNN_8Neigh(in_ch) for _ in range(64//16)])  # hard-coding `64//16`

    def forward(self, x):
        d = x.shape[-1]
        x_hid = []
        for i in range(d):
            hid = self.dag_list[i](x[..., i].clone())  # CAREFUL torch.tensor is mutable, so isn't torch.clone() needed?
            x_hid.append(hid.unsqueeze(-1))
        x_hid = torch.cat(x_hid, dim=-1)

        return x + x_hid


if __name__ == '__main__':
    import time
    import os
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'
    model = UNetL9DeepSupRFP_MultiHead(1, 9, init_ch=16, conv_layer_order='cbr', interpolate=True, num_neigh='four')
    device = torch.device('cuda')
    model = model.to(device)
    summary(model, (1, 160, 160, 64))

    from models.unet_nine_layers.unet_l9 import count_parameters, parameter_table
    print(parameter_table(model))
    print('Total number of trainable parameters: {:.2f} M'.format(count_parameters(model) / 1e6))