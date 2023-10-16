import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from dataset import OASISDataLoader

# Torch configuration
seed = 42
torch.manual_seed(seed)
print(torch.__version__)
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print(device)
print("Name: ", torch.cuda.get_device_name(0))


class VectorQuantizer(nn.Module):
    def __init__(self, num_embeddings, embedding_dim, commitment_loss):
        super(VectorQuantizer, self).__init__()
        self.embedding_dim = embedding_dim
        self.num_embeddings = num_embeddings

        # Initialize the embeddings which we will quantize.
        self.embedding = nn.Embedding(self.num_embeddings, self.embedding_dim)
        self.embedding.weight.data.uniform_(-1 / self.num_embeddings, 1 / self.num_embeddings)
        self.commitment_loss = commitment_loss

    def forward(self, inputs):
        # convert inputs from BCHW -> BHWC
        inputs = inputs.permute(0, 2, 3, 1).contiguous()
        input_shape = inputs.shape

        # Flatten input
        flat_input = inputs.view(-1, self.embedding_dim)

        # Calculate distances
        distances = (torch.sum(flat_input ** 2, dim=1, keepdim=True)
                     + torch.sum(self.embedding.weight ** 2, dim=1)
                     - 2 * torch.matmul(flat_input, self.embedding.weight.t()))

        # Encoding
        encoding_indices = torch.argmin(distances, dim=1).unsqueeze(1)
        encodings = torch.zeros(encoding_indices.shape[0], self.num_embeddings, device=inputs.device)
        encodings.scatter_(1, encoding_indices, 1)

        # Quantize and unflatten
        quantized = torch.matmul(encodings, self.embedding.weight).view(input_shape)

        # Loss
        e_latent_loss = F.mse_loss(quantized.detach(), inputs)
        q_latent_loss = F.mse_loss(quantized, inputs.detach())
        loss = q_latent_loss + self.commitment_loss * e_latent_loss

        quantized = inputs + (quantized - inputs).detach()
        avg_probs = torch.mean(encodings, dim=0)

        # convert quantized from BHWC -> BCHW
        return loss, quantized.permute(0, 3, 1, 2).contiguous(), encodings, encoding_indices


class Residual(nn.Module):
    def __init__(self, in_channels, num_hiddens, num_residual_hiddens):
        super(Residual, self).__init__()
        self._block = nn.Sequential(
            nn.ReLU(True),
            nn.Conv2d(in_channels=in_channels,
                      out_channels=num_residual_hiddens,
                      kernel_size=3, stride=1, padding=1, bias=False),
            nn.ReLU(True),
            nn.Conv2d(in_channels=num_residual_hiddens,
                      out_channels=num_hiddens,
                      kernel_size=1, stride=1, bias=False)
        )

    def forward(self, x):
        return x + self._block(x)


class Encoder(nn.Module):
    def __init__(self, in_channels, num_hiddens, num_residual_hiddens):
        super(Encoder, self).__init__()

        self.conv1 = nn.Conv2d(in_channels=in_channels,
                               out_channels=num_hiddens // 2,
                               kernel_size=4,
                               stride=2, padding=1)
        self.relu1 = nn.ReLU()
        self.conv2 = nn.Conv2d(in_channels=num_hiddens // 2,
                               out_channels=num_hiddens,
                               kernel_size=4,
                               stride=2, padding=1)

        self.residualblock1 = Residual(in_channels=num_hiddens,
                                       num_hiddens=num_hiddens,
                                       num_residual_hiddens=num_residual_hiddens)
        self.residualblock2 = Residual(in_channels=num_hiddens,
                                       num_hiddens=num_hiddens,
                                       num_residual_hiddens=num_residual_hiddens)

    def forward(self, inputs):
        x = self.conv1(inputs)
        x = self.relu1(x)
        x = self.conv2(x)
        x = self.residualblock1(x)
        x = self.residualblock2(x)
        return x


class Decoder(nn.Module):
    def __init__(self, in_channels, num_hiddens, num_residual_hiddens):
        super(Decoder, self).__init__()

        self.conv1 = nn.Conv2d(in_channels=in_channels,
                                 out_channels=num_hiddens,
                                 kernel_size=3,
                                 stride=1, padding=1)

        self.residualblock1 = Residual(in_channels=num_hiddens,
                                       num_hiddens=num_hiddens,
                                       num_residual_hiddens=num_residual_hiddens)
        self.residualblock2 = Residual(in_channels=num_hiddens,
                                       num_hiddens=num_hiddens,
                                       num_residual_hiddens=num_residual_hiddens)
        self.convT1 = nn.ConvTranspose2d(in_channels=num_hiddens,
                                         out_channels=num_hiddens // 2,
                                         kernel_size=4,
                                         stride=2, padding=1)
        self.relu1 = nn.ReLU()
        self.convT2 = nn.ConvTranspose2d(in_channels=num_hiddens // 2,
                                         out_channels=1,
                                         kernel_size=4,
                                         stride=2, padding=1)

    def forward(self, inputs):
        x = self.conv1(inputs)
        x = self.residualblock1(x)
        x = self.residualblock2(x)
        x = self.convT1(x)
        x = self.relu1(x)
        x = self.convT2(x)
        return x


class VQVAE(nn.Module):
    def __init__(self, num_channels, num_hiddens, num_residual_hiddens,
                 num_embeddings, embedding_dim, commitment_cost):
        super(VQVAE, self).__init__()
        self.encoder = Encoder(num_channels,
                               num_hiddens,
                               num_residual_hiddens)
        self.pre_vq_conv = nn.Conv2d(in_channels=num_hiddens,
                                     out_channels=embedding_dim,
                                     kernel_size=1,
                                     stride=1)
        self.vqvae = VectorQuantizer(num_embeddings, embedding_dim,
                                     commitment_cost)

        self.decoder = Decoder(embedding_dim,
                               num_hiddens,
                               num_residual_hiddens)

    def forward(self, inputs):
        x = self.encoder(inputs)
        x = self.pre_vq_conv(x)
        loss, quantized, _, _ = self.vqvae(x)
        x_recon = self.decoder(quantized)

        return loss, x_recon


# Train Model
batch_size = 32
num_epochs = 0
learning_rate = 0.0002
commitment_cost = 0.25
num_hiddens = 128
num_residual_hiddens = 32
num_channels = 1
embedding_dim = 64
num_embeddings = 512

train_loader, test_loader, val_loader = OASISDataLoader(batch_size=batch_size).get_dataloaders()

# Calculate variance
mean = 0.0
meansq = 0.0
count = 0

for index, data in enumerate(train_loader):
    mean = data.sum()
    meansq = meansq + (data**2).sum()
    count += np.prod(data.shape)

total_mean = mean/count
total_var = (meansq/count) - (total_mean**2)
data_variance = float(total_var.item()) # 0.68

model = VQVAE(num_channels, num_hiddens, num_residual_hiddens, num_embeddings, embedding_dim, commitment_cost).to(device)
optimizer = optim.Adam(model.parameters(), lr=learning_rate, amsgrad=False)

train_error = []
model.train()
for epoch in range(num_epochs):
    print(f"Epoch: {epoch}")
    train_loss = 0
    for i, data in enumerate(train_loader):
        data = data.to(device)
        optimizer.zero_grad()

        vq_loss, data_recon = model(data)

        recon_error = F.mse_loss(data_recon, data) / data_variance
        loss = recon_error + vq_loss
        loss.backward()

        optimizer.step()

        train_error.append(recon_error.item())

    train_loss = np.mean(train_error[-300:])
    print('training_loss: %.3f' % train_loss)
