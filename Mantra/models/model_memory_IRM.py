import sys
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
import random

sys.path.append('../../../Mantra/models')
from model_utils import get_tolerance_rate
from model_controllerMem import model_controllerMem

class model_memory_IRM(model_controllerMem):
    """
    Memory Network model with Iterative Refinement Module.
    """

    def __init__(self, settings, model_pretrained):
        super(model_memory_IRM, self).__init__(settings, model_pretrained)
        self.name_model = 'MANTRA'

        # parameters
        self.device = settings["device"]
        self.dim_embedding_key = settings["dim_embedding_key"]
        self.num_prediction = settings["num_prediction"]
        self.past_len = settings["past_len"]
        self.future_len = settings["future_len"]

        # similarity criterion
        self.weight_read = []
        self.index_max = []
        self.similarity = nn.CosineSimilarity(dim=1)

        # Memory
        self.memory_past = model_pretrained.memory_past
        self.memory_fut = model_pretrained.memory_fut
        self.memory_count = []

        # layers
        self.model_controller = model_pretrained
        self.model_ae = self.model_controller.model_ae

        ########################################################################################
        # diesen block braucht man nicht, aber man muss ihn lassen um das model laden zu können
        self.conv_past = model_pretrained.conv_past
        self.conv_fut = model_pretrained.conv_fut
        self.encoder_past = model_pretrained.encoder_past
        self.encoder_fut = model_pretrained.encoder_fut
        self.decoder = model_pretrained.decoder
        self.FC_output = model_pretrained.FC_output

        self.linear_controller = model_pretrained.linear_controller
        ########################################################################################

        self.relu = nn.ReLU()
        self.softmax = nn.Softmax()

        self.maxpool2d = torch.nn.MaxPool2d(kernel_size=2, stride=2)


        # scene: input shape (batch, classes, 360, 360)
        self.num_scene_layers = 2
        self.dim_clip = settings["dim_clip"]
        self.convScene_1 = nn.Sequential(
            nn.Conv2d(in_channels=self.num_scene_layers,
                      out_channels=8,
                      kernel_size=(5, 5),
                      stride=(2, 2),
                      padding=2),
            nn.ReLU(),
            nn.BatchNorm2d(8))

        self.convScene_2 = nn.Sequential(
            nn.Conv2d(in_channels=8,
                      out_channels=16,
                      kernel_size=(5, 5),
                      stride=(1, 1),
                      padding=2),
            nn.ReLU(),
            nn.BatchNorm2d(16))

        self.RNN_scene = nn.GRU(input_size=16,
                                hidden_size=self.dim_embedding_key,
                                num_layers=1,
                                batch_first=True)

        # refinement fc layer
        self.fc_refine = nn.Linear(in_features=self.dim_embedding_key,
                                   out_features=self.future_len * 2)

        self.reset_parameters()


    def reset_parameters(self):
        nn.init.kaiming_normal_(self.RNN_scene.weight_ih_l0)
        nn.init.kaiming_normal_(self.RNN_scene.weight_hh_l0)
        nn.init.kaiming_normal_(self.RNN_scene.weight_ih_l0)
        nn.init.kaiming_normal_(self.RNN_scene.weight_hh_l0)
        nn.init.kaiming_normal_(self.convScene_1[0].weight)
        nn.init.kaiming_normal_(self.convScene_2[0].weight)
        nn.init.kaiming_normal_(self.fc_refine.weight)

        nn.init.zeros_(self.RNN_scene.bias_ih_l0)
        nn.init.zeros_(self.RNN_scene.bias_hh_l0)
        nn.init.zeros_(self.RNN_scene.bias_ih_l0)
        nn.init.zeros_(self.RNN_scene.bias_hh_l0)
        nn.init.zeros_(self.convScene_1[0].bias)
        nn.init.zeros_(self.convScene_2[0].bias)
        nn.init.zeros_(self.fc_refine.bias)

    def predict(self, past, scene=None):
        """
        Forward pass. Refine predictions generated by MemNet with IRM.
        :param past: past trajectory
        :param scene: surrounding map
        :return: predicted future
        """
        prediction, state_past = super(model_memory_IRM, self).predict(past)

        if scene is not None:
            prediction = self.refine_prediction(prediction, scene, state_past)
        return prediction, state_past

    def forward(self, past, future=None, scene=None, visualize_sample=False):
        # past [bs, hl, 2]
        # fut  [bs, ph, 2]
        # pred [bs, k, ph, 2]
        prediction, state_past = self.predict(past, scene)
        if visualize_sample:
            sample_id = 0
            plt.plot(past[sample_id, :, 0], past[sample_id, :, 1], color='b', label='past')
            plt.plot(future[sample_id, :, 0], future[sample_id, :, 1], color='r', label='future')
            future_decoded = self.model_ae(past, future)
            plt.plot(future_decoded[sample_id, :, 0], future_decoded[sample_id, :, 1], color='r', label='future_decoded')

            for k in range(prediction.shape[1]):
                plt.plot(prediction[sample_id, k, :, 0], prediction[sample_id, k, :, 1], color='c', label='pred')
            plt.legend(loc='best')
            plt.axis('equal')
            plt.show()

        if future is not None:
            writing_prob, tolerance_rate = self.write_in_memory(prediction, future, state_past)
            return writing_prob, tolerance_rate
        else:
            return prediction

    def refine_prediction(self, prediction, scene, state_past):
        # scene encoding
        scene = scene.permute(0, 3, 1, 2)
        scene_1 = self.convScene_1(scene)
        scene_2 = self.convScene_2(scene_1)
        scene_2 = scene_2.repeat_interleave(self.num_prediction, dim=0)
        # Iteratively refine predictions using context
        bs, k, ph, dim = prediction.shape
        prediction = prediction.view(bs * k, ph, 2)
        for i_refine in range(4):
            pred_map = prediction + self.dim_clip / 2
            pred_map = pred_map.unsqueeze(2)
            indices = pred_map.permute(0, 2, 1, 3)
            # rescale between -1 and 1
            indices = 2 * (indices / self.dim_clip) - 1
            output = F.grid_sample(scene_2, indices, mode='nearest', align_corners=True)
            output = output.squeeze(2).permute(0, 2, 1)

            state_rnn = state_past.repeat_interleave(self.num_prediction, dim=1)

            output_rnn, state_rnn = self.RNN_scene(output, state_rnn)
            prediction_refine = self.fc_refine(state_rnn).view(-1, ph, dim)
            prediction = prediction + prediction_refine

        prediction = prediction.view(bs, k, ph, dim)
        return prediction
