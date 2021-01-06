import datetime

import tensorflow as tf
import tensorflow_addons as tfa
import numpy as np
import logging
import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import (
    List,
    Text,
    Dict,
    Tuple,
    Union,
    Optional,
    Callable,
    TYPE_CHECKING,
    Any,
)

from tqdm import tqdm
from rasa.constants import CHECKPOINT_MODEL_NAME
from rasa.shared.utils.io import is_logging_disabled
import rasa.utils.io
from rasa.utils.tensorflow.model_data import RasaModelData, FeatureSignature
from rasa.utils.tensorflow.constants import (
    SEQUENCE,
    SENTENCE,
    TENSORBOARD_LOG_LEVEL,
    RANDOM_SEED,
    TENSORBOARD_LOG_DIR,
    CHECKPOINT_MODEL,
    EMBEDDING_DIMENSION,
    REGULARIZATION_CONSTANT,
    SIMILARITY_TYPE,
    WEIGHT_SPARSITY,
    NUM_TRANSFORMER_LAYERS,
    TRANSFORMER_SIZE,
    NUM_HEADS,
    UNIDIRECTIONAL_ENCODER,
    KEY_RELATIVE_ATTENTION,
    VALUE_RELATIVE_ATTENTION,
    MAX_RELATIVE_POSITION,
    NUM_NEG,
    LOSS_TYPE,
    MAX_POS_SIM,
    MAX_NEG_SIM,
    USE_MAX_NEG_SIM,
    NEGATIVE_MARGIN_SCALE,
    HIDDEN_LAYERS_SIZES,
    DROP_RATE,
    DENSE_DIMENSION,
    CONCAT_DIMENSION,
    DROP_RATE_ATTENTION,
    SCALE_LOSS,
    CONSTRAIN_SIMILARITIES,
)
from rasa.utils.tensorflow import layers
from rasa.utils.tensorflow.transformer import TransformerEncoder

if TYPE_CHECKING:
    from tensorflow.python.ops.summary_ops_v2 import ResourceSummaryWriter

logger = logging.getLogger(__name__)


TENSORBOARD_LOG_LEVELS = ["epoch", "minibatch"]


# noinspection PyMethodOverriding
class RasaModel(tf.keras.models.Model):
    """Completely override all public methods of keras Model.

    Cannot be used as tf.keras.Model
    """

    def __init__(
        self,
        random_seed: Optional[int] = None,
        tensorboard_log_dir: Optional[Text] = None,
        tensorboard_log_level: Optional[Text] = "epoch",
        checkpoint_model: Optional[bool] = False,
        **kwargs,
    ) -> None:
        """Initialize the RasaModel.

        Args:
            random_seed: set the random seed to get reproducible results
        """
        super().__init__(**kwargs)

        self.total_loss = tf.keras.metrics.Mean(name="t_loss")
        self.metrics_to_log = ["t_loss"]

        self._training = None  # training phase should be defined when building a graph

        self._predict_function = None

        self.random_seed = random_seed

        self.tensorboard_log_dir = tensorboard_log_dir
        self.tensorboard_log_level = tensorboard_log_level

        self.train_summary_writer = None
        self.test_summary_writer = None
        self.model_summary_file = None
        self.tensorboard_log_on_epochs = True

        self.best_metrics_so_far = {}
        self.checkpoint_model = checkpoint_model
        self.best_model_file = None
        self.best_model_epoch = -1
        if self.checkpoint_model:
            model_checkpoint_dir = rasa.utils.io.create_temporary_directory()
            self.best_model_file = os.path.join(
                model_checkpoint_dir, f"{CHECKPOINT_MODEL_NAME}.tf_model"
            )

    def _set_up_tensorboard_writer(self) -> None:
        if self.tensorboard_log_dir is not None:
            if self.tensorboard_log_level not in TENSORBOARD_LOG_LEVELS:
                raise ValueError(
                    f"Provided '{TENSORBOARD_LOG_LEVEL}' "
                    f"('{self.tensorboard_log_level}') "
                    f"is invalid! Valid values are: {TENSORBOARD_LOG_LEVELS}"
                )
            self.tensorboard_log_on_epochs = self.tensorboard_log_level == "epoch"

            current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            class_name = self.__class__.__name__

            train_log_dir = (
                f"{self.tensorboard_log_dir}/{class_name}/{current_time}/train"
            )
            test_log_dir = (
                f"{self.tensorboard_log_dir}/{class_name}/{current_time}/test"
            )

            self.train_summary_writer = tf.summary.create_file_writer(train_log_dir)
            self.test_summary_writer = tf.summary.create_file_writer(test_log_dir)

            self.model_summary_file = (
                f"{self.tensorboard_log_dir}/{class_name}/{current_time}"
                f"/model_summary.txt"
            )

    def batch_loss(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> tf.Tensor:
        """Calculates the loss for the given batch.

        Args:
            batch_in: The batch.

        Returns:
            The loss of the given batch.
        """
        raise NotImplementedError

    def prepare_for_predict(self) -> None:
        """Prepares tf graph fpr prediction.

        This method should contain necessary tf calculations
        and set self variables that are used in `batch_predict`.
        For example, pre calculation of `self.all_labels_embed`.
        """
        pass

    def batch_predict(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> Dict[Text, tf.Tensor]:
        """Predicts the output of the given batch.

        Args:
            batch_in: The batch.

        Returns:
            The output to predict.
        """
        raise NotImplementedError

    def fit(
        self,
        model_data: RasaModelData,
        epochs: int,
        batch_size: Union[List[int], int],
        evaluate_on_num_examples: int,
        evaluate_every_num_epochs: int,
        batch_strategy: Text,
        silent: bool = False,
        loading: bool = False,
        eager: bool = False,
    ) -> None:
        """Fit model data."""
        # don't setup tensorboard writers when training during loading
        if not loading:
            self._set_up_tensorboard_writer()

        tf.random.set_seed(self.random_seed)
        np.random.seed(self.random_seed)

        disable = silent or is_logging_disabled()

        evaluation_model_data = None
        if evaluate_on_num_examples > 0:
            if not disable:
                logger.info(
                    f"Validation accuracy is calculated every "
                    f"{evaluate_every_num_epochs} epochs."
                )

            model_data, evaluation_model_data = model_data.split(
                evaluate_on_num_examples, self.random_seed
            )

        (
            train_dataset_function,
            tf_train_on_batch_function,
        ) = self._get_tf_train_functions(eager, model_data, batch_strategy)
        (
            evaluation_dataset_function,
            tf_evaluation_on_batch_function,
        ) = self._get_tf_evaluation_functions(eager, evaluation_model_data)

        val_results = {}  # validation is not performed every epoch
        progress_bar = tqdm(range(epochs), desc="Epochs", disable=disable)

        training_steps = 0

        for epoch in progress_bar:
            epoch_batch_size = self.linearly_increasing_batch_size(
                epoch, batch_size, epochs
            )

            training_steps = self._batch_loop(
                train_dataset_function,
                tf_train_on_batch_function,
                epoch_batch_size,
                True,
                training_steps,
                self.train_summary_writer,
            )

            if self.tensorboard_log_on_epochs:
                self._log_metrics_for_tensorboard(epoch, self.train_summary_writer)

            postfix_dict = self._get_metric_results()

            if evaluate_on_num_examples > 0:
                if self._should_evaluate(evaluate_every_num_epochs, epochs, epoch):
                    self._batch_loop(
                        evaluation_dataset_function,
                        tf_evaluation_on_batch_function,
                        epoch_batch_size,
                        False,
                        training_steps,
                        self.test_summary_writer,
                    )

                    if self.tensorboard_log_on_epochs:
                        self._log_metrics_for_tensorboard(
                            epoch, self.test_summary_writer
                        )

                    val_results = self._get_metric_results(prefix="val_")
                    self._save_model_checkpoint(
                        current_results=val_results, epoch=epoch
                    )

                postfix_dict.update(val_results)

            progress_bar.set_postfix(postfix_dict)

        if self.checkpoint_model:
            logger.info(
                f"The model of epoch {self.best_model_epoch} "
                f"(out of {epochs} in total) will be stored!"
            )
        if self.model_summary_file is not None:
            self._write_model_summary()

        self._training = None  # training phase should be defined when building a graph
        if not disable:
            logger.info("Finished training.")

    def train_on_batch(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> None:
        """Train on batch."""
        # calculate supervision and regularization losses separately
        with tf.GradientTape(persistent=True) as tape:
            prediction_loss = self.batch_loss(batch_in)
            regularization_loss = tf.math.add_n(self.losses)
            total_loss = prediction_loss + regularization_loss

        self.total_loss.update_state(total_loss)

        # calculate the gradients that come from supervision signal
        prediction_gradients = tape.gradient(prediction_loss, self.trainable_variables)
        # calculate the gradients that come from regularization
        regularization_gradients = tape.gradient(
            regularization_loss, self.trainable_variables
        )
        # delete gradient tape manually
        # since it was created with `persistent=True` option
        del tape

        gradients = []
        for pred_grad, reg_grad in zip(prediction_gradients, regularization_gradients):
            if pred_grad is not None and reg_grad is not None:
                # remove regularization gradient for variables
                # that don't have prediction gradient
                gradients.append(
                    pred_grad
                    + tf.where(pred_grad > 0, reg_grad, tf.zeros_like(reg_grad))
                )
            else:
                gradients.append(pred_grad)

        self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))

    def build_for_predict(
        self, predict_data: RasaModelData, eager: bool = False
    ) -> None:
        self._training = False  # needed for tf graph mode
        self.prepare_for_predict()
        self._predict_function = self._get_tf_call_model_function(
            predict_data.as_tf_dataset, self.batch_predict, eager, "prediction"
        )

    def predict(self, predict_data: RasaModelData) -> Dict[Text, tf.Tensor]:
        if self._predict_function is None:
            logger.debug("There is no tensorflow prediction graph.")
            self.build_for_predict(predict_data)

        # Prepare a single batch of the size of the input
        batch_in = predict_data.prepare_batch()

        self._training = False  # needed for eager mode
        return self._predict_function(batch_in)

    def save(self, model_file_name: Text, overwrite: bool = True) -> None:
        self.save_weights(model_file_name, overwrite=overwrite, save_format="tf")

    def copy_best(self, model_file_name: Text) -> None:
        checkpoint_directory, checkpoint_file = os.path.split(self.best_model_file)
        checkpoint_path = Path(checkpoint_directory)

        # Copy all tf2 model files from the temp location to the final destination
        for f in checkpoint_path.glob(f"{checkpoint_file}*"):
            shutil.move(str(f.absolute()), model_file_name + f.suffix)

        # Generate the tf2 checkpoint file, copy+replace to ensure consistency
        destination_path, destination_file = os.path.split(model_file_name)
        with open(os.path.join(checkpoint_directory, "checkpoint")) as in_file, open(
            os.path.join(destination_path, "checkpoint"), "w"
        ) as out_file:
            for line in in_file:
                out_file.write(line.replace(checkpoint_file, destination_file))

        # Remove the old file
        checkpoint_path.joinpath("checkpoint").unlink()

    @classmethod
    def load(
        cls,
        model_file_name: Text,
        model_data_example: RasaModelData,
        finetune_mode: bool = False,
        *args,
        **kwargs,
    ) -> "RasaModel":
        """Loads a model from the given weights.

        Args:
            model_file_name: Path to file containing model weights.
            model_data_example: Example data point to construct the model architecture.
            finetune_mode: Indicates whether to load the model for further finetuning.
            *args: Any other non key-worded arguments.
            **kwargs: Any other key-worded arguments.

        Returns:
            Loaded model with weights appropriately set.
        """
        logger.debug(
            f"Loading the model from {model_file_name} with finetune_mode={finetune_mode}..."
        )
        # create empty model
        model = cls(*args, **kwargs)
        # need to train on 1 example to build weights of the correct size
        model.fit(
            model_data_example,
            epochs=1,
            batch_size=1,
            evaluate_every_num_epochs=0,
            evaluate_on_num_examples=0,
            batch_strategy=SEQUENCE,
            silent=True,  # don't confuse users with training output
            loading=True,  # don't use tensorboard when doing a dummy fit run
            eager=(
                False if finetune_mode else True
            ),  # load in eager mode only for prediction phase
        )
        # load trained weights
        model.load_weights(model_file_name)

        logger.debug("Finished loading the model.")
        return model

    def _total_batch_loss(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> tf.Tensor:
        """Calculate total loss."""
        prediction_loss = self.batch_loss(batch_in)
        regularization_loss = tf.math.add_n(self.losses)
        total_loss = prediction_loss + regularization_loss
        self.total_loss.update_state(total_loss)

        return total_loss

    def _batch_loop(
        self,
        dataset_function: Callable,
        call_model_function: Callable,
        batch_size: int,
        training: bool,
        offset: int,
        writer: Optional["ResourceSummaryWriter"] = None,
    ) -> int:
        """Run on batches."""
        self.reset_metrics()

        step = offset

        self._training = training  # needed for eager mode
        for batch_in in dataset_function(batch_size):
            call_model_function(batch_in)

            if not self.tensorboard_log_on_epochs:
                self._log_metrics_for_tensorboard(step, writer)

            step += 1

        return step

    @staticmethod
    def _get_tf_call_model_function(
        dataset_function: Callable,
        call_model_function: Callable,
        eager: bool,
        phase: Text,
    ) -> Callable:
        """Convert functions to tensorflow functions."""
        if eager:
            return call_model_function

        logger.debug(f"Building tensorflow {phase} graph...")

        init_dataset = dataset_function(1)
        tf_call_model_function = tf.function(
            call_model_function, input_signature=[init_dataset.element_spec]
        )
        tf_call_model_function(next(iter(init_dataset)))

        logger.debug(f"Finished building tensorflow {phase} graph.")

        return tf_call_model_function

    def _get_tf_train_functions(
        self, eager: bool, model_data: RasaModelData, batch_strategy: Text
    ) -> Tuple[Callable, Callable]:
        """Create train tensorflow functions."""

        def train_dataset_function(_batch_size: int) -> tf.data.Dataset:
            return model_data.as_tf_dataset(_batch_size, batch_strategy, shuffle=True)

        self._training = True  # needed for tf graph mode
        return (
            train_dataset_function,
            self._get_tf_call_model_function(
                train_dataset_function, self.train_on_batch, eager, "train"
            ),
        )

    def _get_tf_evaluation_functions(
        self, eager: bool, evaluation_model_data: Optional[RasaModelData]
    ) -> Tuple[Optional[Callable], Optional[Callable]]:
        """Create evaluation tensorflow functions."""
        if evaluation_model_data is None:
            return None, None

        def evaluation_dataset_function(_batch_size: int) -> tf.data.Dataset:
            return evaluation_model_data.as_tf_dataset(
                _batch_size, SEQUENCE, shuffle=False
            )

        self._training = False  # needed for tf graph mode
        return (
            evaluation_dataset_function,
            self._get_tf_call_model_function(
                evaluation_dataset_function, self._total_batch_loss, eager, "evaluation"
            ),
        )

    def _get_metric_results(self, prefix: Optional[Text] = None) -> Dict[Text, Text]:
        """Get the metrics results"""
        prefix = prefix or ""

        return {
            f"{prefix}{metric.name}": f"{metric.result().numpy():.3f}"
            for metric in self.metrics
            if metric.name in self.metrics_to_log
        }

    def _log_metrics_for_tensorboard(
        self, step: int, writer: Optional["ResourceSummaryWriter"] = None
    ) -> None:
        if writer is not None:
            with writer.as_default():
                for metric in self.metrics:
                    if metric.name in self.metrics_to_log:
                        tf.summary.scalar(metric.name, metric.result(), step=step)

    def _does_model_improve(self, current_results: Dict[Text, Text]) -> bool:
        # Initialize best_metrics_so_far with the first results
        if not self.best_metrics_so_far:
            keys = filter(
                lambda k: True if (k.endswith("_acc") or k.endswith("_f1")) else False,
                current_results.keys(),
            )
            for key in keys:
                self.best_metrics_so_far[key] = float(current_results[key])
            return True

        all_improved = all(
            [
                float(current_results[key]) > self.best_metrics_so_far[key]
                for key in self.best_metrics_so_far.keys()
            ]
        )
        if all_improved:
            for key in self.best_metrics_so_far.keys():
                self.best_metrics_so_far[key] = float(current_results[key])
        return all_improved

    def _save_model_checkpoint(
        self, current_results: Dict[Text, Text], epoch: int
    ) -> None:
        if self.checkpoint_model and self._does_model_improve(current_results):
            logger.debug(f"Creating model checkpoint at epoch={epoch + 1}...")
            self.best_model_epoch = epoch + 1
            self.save(self.best_model_file, overwrite=True)

    @staticmethod
    def _should_evaluate(
        evaluate_every_num_epochs: int, epochs: int, current_epoch: int
    ) -> bool:
        return (
            current_epoch == 0
            or (current_epoch + 1) % evaluate_every_num_epochs == 0
            or (current_epoch + 1) == epochs
        )

    @staticmethod
    def batch_to_model_data_format(
        batch: Union[Tuple[tf.Tensor], Tuple[np.ndarray]],
        data_signature: Dict[Text, Dict[Text, List[FeatureSignature]]],
    ) -> Dict[Text, Dict[Text, List[tf.Tensor]]]:
        """Convert input batch tensors into batch data format.

        Batch contains any number of batch data. The order is equal to the
        key-value pairs in session data. As sparse data were converted into indices,
        data, shape before, this methods converts them into sparse tensors. Dense data
        is kept.
        """
        batch_data = defaultdict(lambda: defaultdict(list))

        idx = 0
        for key, values in data_signature.items():
            for sub_key, signature in values.items():
                for is_sparse, feature_dimension, number_of_dimensions in signature:
                    number_of_dimensions = (
                        number_of_dimensions if number_of_dimensions != 4 else 3
                    )
                    if is_sparse:
                        # explicitly substitute last dimension in shape with known
                        # static value
                        shape = [
                            batch[idx + 2][i] for i in range(number_of_dimensions - 1)
                        ] + [feature_dimension]
                        batch_data[key][sub_key].append(
                            tf.SparseTensor(batch[idx], batch[idx + 1], shape)
                        )
                        idx += 3
                    else:
                        if isinstance(batch[idx], tf.Tensor):
                            batch_data[key][sub_key].append(batch[idx])
                        else:
                            # convert to Tensor
                            batch_data[key][sub_key].append(
                                tf.constant(batch[idx], dtype=tf.float32)
                            )
                        idx += 1

        return batch_data

    @staticmethod
    def linearly_increasing_batch_size(
        epoch: int, batch_size: Union[List[int], int], epochs: int
    ) -> int:
        """Linearly increase batch size with every epoch.

        The idea comes from https://arxiv.org/abs/1711.00489.
        """
        if not isinstance(batch_size, list):
            return int(batch_size)

        if epochs > 1:
            return int(
                batch_size[0] + epoch * (batch_size[1] - batch_size[0]) / (epochs - 1)
            )
        else:
            return int(batch_size[0])

    def _write_model_summary(self):
        total_number_of_variables = np.sum(
            [np.prod(v.shape) for v in self.trainable_variables]
        )
        layers = [
            f"{layer.name} ({layer.dtype.name}) "
            f"[{'x'.join(str(s) for s in layer.shape)}]"
            for layer in self.trainable_variables
        ]
        layers.reverse()

        with open(self.model_summary_file, "w") as file:
            file.write("Variables: name (type) [shape]\n\n")
            for layer in layers:
                file.write(layer)
                file.write("\n")
            file.write("\n")
            file.write(f"Total size of variables: {total_number_of_variables}")

    def compile(self, *args, **kwargs) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )

    def evaluate(self, *args, **kwargs) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )

    def test_on_batch(self, *args, **kwargs) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )

    def predict_on_batch(self, *args, **kwargs) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )

    def fit_generator(self, *args, **kwargs) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )

    def evaluate_generator(self, *args, **kwargs) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )

    def predict_generator(self, *args, **kwargs) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )

    def call(self, *args, **kwargs) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )

    def get_config(self) -> None:
        raise Exception(
            "This method should neither be called nor implemented in our code."
        )


# noinspection PyMethodOverriding
class TransformerRasaModel(RasaModel):
    def __init__(
        self,
        name: Text,
        config: Dict[Text, Any],
        data_signature: Dict[Text, Dict[Text, List[FeatureSignature]]],
        label_data: RasaModelData,
    ) -> None:
        super().__init__(
            name=name,
            random_seed=config[RANDOM_SEED],
            tensorboard_log_dir=config[TENSORBOARD_LOG_DIR],
            tensorboard_log_level=config[TENSORBOARD_LOG_LEVEL],
            checkpoint_model=config[CHECKPOINT_MODEL],
        )

        self.config = config
        self.data_signature = data_signature
        self.label_signature = label_data.get_signature()

        self._check_data()

        label_batch = label_data.prepare_batch()
        self.tf_label_data = self.batch_to_model_data_format(
            label_batch, self.label_signature
        )

        # set up tf layers
        self._tf_layers: Dict[Text, tf.keras.layers.Layer] = {}

    def _check_data(self) -> None:
        raise NotImplementedError

    def _prepare_layers(self) -> None:
        raise NotImplementedError

    def _prepare_embed_layers(self, name: Text, prefix: Text = "embed") -> None:
        self._tf_layers[f"{prefix}.{name}"] = layers.Embed(
            self.config[EMBEDDING_DIMENSION],
            self.config[REGULARIZATION_CONSTANT],
            name,
            self.config[SIMILARITY_TYPE],
        )

    def _prepare_ffnn_layer(
        self,
        name: Text,
        layer_sizes: List[int],
        drop_rate: float,
        prefix: Text = "ffnn",
    ) -> None:
        self._tf_layers[f"{prefix}.{name}"] = layers.Ffnn(
            layer_sizes,
            drop_rate,
            self.config[REGULARIZATION_CONSTANT],
            self.config[WEIGHT_SPARSITY],
            layer_name_suffix=name,
        )

    def _prepare_transformer_layer(
        self,
        name: Text,
        num_layers: int,
        units: int,
        drop_rate: float,
        drop_rate_attention: float,
        prefix: Text = "transformer",
    ):
        if num_layers > 0:
            self._tf_layers[f"{prefix}.{name}"] = TransformerEncoder(
                num_layers,
                units,
                self.config[NUM_HEADS],
                units * 4,
                self.config[REGULARIZATION_CONSTANT],
                dropout_rate=drop_rate,
                attention_dropout_rate=drop_rate_attention,
                sparsity=self.config[WEIGHT_SPARSITY],
                unidirectional=self.config[UNIDIRECTIONAL_ENCODER],
                use_key_relative_position=self.config[KEY_RELATIVE_ATTENTION],
                use_value_relative_position=self.config[VALUE_RELATIVE_ATTENTION],
                max_relative_position=self.config[MAX_RELATIVE_POSITION],
                name=f"{name}_encoder",
            )
        else:
            # create lambda so that it can be used later without the check
            self._tf_layers[f"{prefix}.{name}"] = lambda x, mask, training: x

    def _prepare_dot_product_loss(
        self, name: Text, scale_loss: bool, prefix: Text = "loss"
    ) -> None:
        self._tf_layers[f"{prefix}.{name}"] = layers.DotProductLoss(
            self.config[NUM_NEG],
            self.config[LOSS_TYPE],
            self.config[MAX_POS_SIM],
            self.config[MAX_NEG_SIM],
            self.config[USE_MAX_NEG_SIM],
            self.config[NEGATIVE_MARGIN_SCALE],
            scale_loss,
            # set to 1 to get deterministic behaviour
            parallel_iterations=1 if self.random_seed is not None else 1000,
            constrain_similarities=self.config[CONSTRAIN_SIMILARITIES],
        )

    def _prepare_sparse_dense_dropout_layers(
        self, name: Text, drop_rate: float
    ) -> None:
        self._tf_layers[f"sparse_input_dropout.{name}"] = layers.SparseDropout(
            rate=drop_rate
        )
        self._tf_layers[f"dense_input_dropout.{name}"] = tf.keras.layers.Dropout(
            rate=drop_rate
        )

    def _prepare_sparse_dense_layers(
        self, data_signature: List[FeatureSignature], name: Text, dense_dim: int
    ) -> None:
        sparse = False
        dense = False
        for is_sparse, _, _ in data_signature:
            if is_sparse:
                sparse = True
            else:
                dense = True

        if sparse:
            self._tf_layers[f"sparse_to_dense.{name}"] = layers.DenseForSparse(
                units=dense_dim,
                reg_lambda=self.config[REGULARIZATION_CONSTANT],
                name=name,
            )
            if not dense:
                # create dense labels for the input to use in negative sampling
                self._tf_layers[f"sparse_to_dense_ids.{name}"] = layers.DenseForSparse(
                    units=2,
                    use_bias=False,
                    trainable=False,
                    name=f"sparse_to_dense_ids.{name}",
                )

    def _prepare_input_layers(self, name: Text) -> None:
        self._prepare_ffnn_layer(
            name, self.config[HIDDEN_LAYERS_SIZES][name], self.config[DROP_RATE]
        )

        for feature_type in [SENTENCE, SEQUENCE]:
            if (
                name not in self.data_signature
                or feature_type not in self.data_signature[name]
            ):
                continue

            self._prepare_sparse_dense_dropout_layers(
                f"{name}_{feature_type}", self.config[DROP_RATE]
            )
            self._prepare_sparse_dense_layers(
                self.data_signature[name][feature_type],
                f"{name}_{feature_type}",
                self.config[DENSE_DIMENSION][name],
            )
            self._prepare_ffnn_layer(
                f"{name}_{feature_type}",
                [self.config[CONCAT_DIMENSION][name]],
                self.config[DROP_RATE],
                prefix="concat_layer",
            )

    def _prepare_sequence_layers(self, name: Text) -> None:
        self._prepare_input_layers(name)

        size = self.config[TRANSFORMER_SIZE]
        if isinstance(size, dict):
            size = size[name]

        num_layers = self.config[NUM_TRANSFORMER_LAYERS]
        if isinstance(num_layers, dict):
            num_layers = num_layers[name]

        self._prepare_transformer_layer(
            name,
            num_layers,
            size,
            self.config[DROP_RATE],
            self.config[DROP_RATE_ATTENTION],
        )

    def _prepare_entity_recognition_layers(self) -> None:
        for tag_spec in self._entity_tag_specs:
            name = tag_spec.tag_name
            num_tags = tag_spec.num_tags
            self._tf_layers[f"embed.{name}.logits"] = layers.Embed(
                num_tags, self.config[REGULARIZATION_CONSTANT], f"logits.{name}"
            )
            self._tf_layers[f"crf.{name}"] = layers.CRF(
                num_tags, self.config[REGULARIZATION_CONSTANT], self.config[SCALE_LOSS]
            )
            self._tf_layers[f"embed.{name}.tags"] = layers.Embed(
                self.config[EMBEDDING_DIMENSION],
                self.config[REGULARIZATION_CONSTANT],
                f"tags.{name}",
            )

    def _combine_sparse_dense_features(
        self,
        features: List[Union[np.ndarray, tf.Tensor, tf.SparseTensor]],
        name: Text,
        mask: Optional[tf.Tensor] = None,
        sparse_dropout: bool = False,
        dense_dropout: bool = False,
    ) -> Optional[tf.Tensor]:
        if not features:
            return None

        dense_features = []

        for f in features:
            if isinstance(f, tf.SparseTensor):
                if sparse_dropout:
                    _f = self._tf_layers[f"sparse_input_dropout.{name}"](
                        f, self._training
                    )
                else:
                    _f = f

                dense_f = self._tf_layers[f"sparse_to_dense.{name}"](_f)

                if dense_dropout:
                    dense_f = self._tf_layers[f"dense_input_dropout.{name}"](
                        dense_f, self._training
                    )

                dense_features.append(dense_f)
            else:
                dense_features.append(f)

        if mask is None:
            return tf.concat(dense_features, axis=-1)

        return tf.concat(dense_features, axis=-1) * mask

    def _combine_sequence_sentence_features(
        self,
        sequence_features: List[Union[tf.Tensor, tf.SparseTensor]],
        sentence_features: List[Union[tf.Tensor, tf.SparseTensor]],
        mask_sequence: tf.Tensor,
        mask_text: tf.Tensor,
        name: Text,
        sparse_dropout: bool = False,
        dense_dropout: bool = False,
    ) -> tf.Tensor:
        sequence_x = self._combine_sparse_dense_features(
            sequence_features,
            f"{name}_{SEQUENCE}",
            mask_sequence,
            sparse_dropout,
            dense_dropout,
        )
        sentence_x = self._combine_sparse_dense_features(
            sentence_features, f"{name}_{SENTENCE}", None, sparse_dropout, dense_dropout
        )

        if sequence_x is not None and sentence_x is None:
            return sequence_x

        if sequence_x is None and sentence_x is not None:
            return sentence_x

        if sequence_x is not None and sentence_x is not None:
            return self._concat_sequence_sentence_features(
                sequence_x, sentence_x, name, mask_text
            )

        raise ValueError(
            "No features are present. Please check your configuration file."
        )

    def _concat_sequence_sentence_features(
        self,
        sequence_x: tf.Tensor,
        sentence_x: tf.Tensor,
        name: Text,
        mask_text: tf.Tensor,
    ):
        if sequence_x.shape[-1] != sentence_x.shape[-1]:
            sequence_x = self._tf_layers[f"concat_layer.{name}_{SEQUENCE}"](
                sequence_x, self._training
            )
            sentence_x = self._tf_layers[f"concat_layer.{name}_{SENTENCE}"](
                sentence_x, self._training
            )

        # we need to concatenate the sequence features with the sentence features
        # we cannot use tf.concat as the sequence features are padded

        # (1) get position of sentence features in mask
        last = mask_text * tf.math.cumprod(
            1 - mask_text, axis=1, exclusive=True, reverse=True
        )
        # (2) multiply by sentence features so that we get a matrix of
        #     batch-dim x seq-dim x feature-dim with zeros everywhere except for
        #     for the sentence features
        sentence_x = last * sentence_x

        # (3) add a zero to the end of sequence matrix to match the final shape
        sequence_x = tf.pad(sequence_x, [[0, 0], [0, 1], [0, 0]])

        # (4) sum up sequence features and sentence features
        return sequence_x + sentence_x

    def _features_as_seq_ids(
        self, features: List[Union[np.ndarray, tf.Tensor, tf.SparseTensor]], name: Text
    ) -> Optional[tf.Tensor]:
        """Creates dense labels for negative sampling."""
        # if there are dense features - we can use them
        for f in features:
            if not isinstance(f, tf.SparseTensor):
                seq_ids = tf.stop_gradient(f)
                # add a zero to the seq dimension for the sentence features
                seq_ids = tf.pad(seq_ids, [[0, 0], [0, 1], [0, 0]])
                return seq_ids

        # use additional sparse to dense layer
        for f in features:
            if isinstance(f, tf.SparseTensor):
                seq_ids = tf.stop_gradient(
                    self._tf_layers[f"sparse_to_dense_ids.{name}"](f)
                )
                # add a zero to the seq dimension for the sentence features
                seq_ids = tf.pad(seq_ids, [[0, 0], [0, 1], [0, 0]])
                return seq_ids

        return None

    def _create_sequence(
        self,
        sequence_features: List[Union[tf.Tensor, tf.SparseTensor]],
        sentence_features: List[Union[tf.Tensor, tf.SparseTensor]],
        mask_sequence: tf.Tensor,
        mask: tf.Tensor,
        name: Text,
        sparse_dropout: bool = False,
        dense_dropout: bool = False,
        masked_lm_loss: bool = False,
        sequence_ids: bool = False,
    ) -> Tuple[tf.Tensor, tf.Tensor, Optional[tf.Tensor], Optional[tf.Tensor]]:
        if sequence_ids:
            seq_ids = self._features_as_seq_ids(sequence_features, f"{name}_{SEQUENCE}")
        else:
            seq_ids = None

        inputs = self._combine_sequence_sentence_features(
            sequence_features,
            sentence_features,
            mask_sequence,
            mask,
            name,
            sparse_dropout,
            dense_dropout,
        )
        inputs = self._tf_layers[f"ffnn.{name}"](inputs, self._training)

        if masked_lm_loss:
            transformer_inputs, lm_mask_bool = self._tf_layers[f"{name}_input_mask"](
                inputs, mask, self._training
            )
        else:
            transformer_inputs = inputs
            lm_mask_bool = None

        outputs = self._tf_layers[f"transformer.{name}"](
            transformer_inputs, 1 - mask, self._training
        )

        if isinstance(self.config[NUM_TRANSFORMER_LAYERS], int):
            num_layers = self.config[NUM_TRANSFORMER_LAYERS]
        else:
            num_layers = self.config[NUM_TRANSFORMER_LAYERS][name]

        if num_layers > 0:
            # apply activation
            outputs = tfa.activations.gelu(outputs)

        return outputs, inputs, seq_ids, lm_mask_bool

    @staticmethod
    def _compute_mask(sequence_lengths: tf.Tensor) -> tf.Tensor:
        mask = tf.sequence_mask(sequence_lengths, dtype=tf.float32)
        # explicitly add last dimension to mask
        # to track correctly dynamic sequences
        return tf.expand_dims(mask, -1)

    @staticmethod
    def _last_token(x: tf.Tensor, sequence_lengths: tf.Tensor) -> tf.Tensor:
        last_sequence_index = tf.maximum(0, sequence_lengths - 1)
        batch_index = tf.range(tf.shape(last_sequence_index)[0])

        indices = tf.stack([batch_index, last_sequence_index], axis=1)
        return tf.gather_nd(x, indices)

    def _get_mask_for(
        self,
        tf_batch_data: Dict[Text, Dict[Text, List[tf.Tensor]]],
        key: Text,
        sub_key: Text,
    ) -> Optional[tf.Tensor]:
        if key not in tf_batch_data or sub_key not in tf_batch_data[key]:
            return None

        sequence_lengths = tf.cast(tf_batch_data[key][sub_key][0], dtype=tf.int32)
        return self._compute_mask(sequence_lengths)

    @staticmethod
    def _get_sequence_lengths(
        tf_batch_data: Dict[Text, Dict[Text, List[tf.Tensor]]],
        key: Text,
        sub_key: Text,
        batch_dim: int = 1,
    ) -> tf.Tensor:
        # sentence features have a sequence lengths of 1
        # if sequence features are present we add the sequence lengths of those

        sequence_lengths = tf.ones([batch_dim], dtype=tf.int32)
        if key in tf_batch_data and sub_key in tf_batch_data[key]:
            sequence_lengths += tf.cast(tf_batch_data[key][sub_key][0], dtype=tf.int32)

        return tf.cast(tf_batch_data[key][sub_key][0], dtype=tf.int32) + 1

    @staticmethod
    def _get_batch_dim(attribute_data: Dict[Text, List[tf.Tensor]]) -> int:
        if SEQUENCE in attribute_data:
            return tf.shape(attribute_data[SEQUENCE][0])[0]

        return tf.shape(attribute_data[SENTENCE][0])[0]

    def _calculate_entity_loss(
        self,
        inputs: tf.Tensor,
        tag_ids: tf.Tensor,
        mask: tf.Tensor,
        sequence_lengths: tf.Tensor,
        tag_name: Text,
        entity_tags: Optional[tf.Tensor] = None,
    ) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:

        tag_ids = tf.cast(tag_ids[:, :, 0], tf.int32)

        if entity_tags is not None:
            _tags = self._tf_layers[f"embed.{tag_name}.tags"](entity_tags)
            inputs = tf.concat([inputs, _tags], axis=-1)

        logits = self._tf_layers[f"embed.{tag_name}.logits"](inputs)

        # should call first to build weights
        pred_ids, _ = self._tf_layers[f"crf.{tag_name}"](logits, sequence_lengths)
        loss = self._tf_layers[f"crf.{tag_name}"].loss(
            logits, tag_ids, sequence_lengths
        )
        f1 = self._tf_layers[f"crf.{tag_name}"].f1_score(tag_ids, pred_ids, mask)

        return loss, f1, logits

    def batch_loss(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> tf.Tensor:
        """Calculates the loss for the given batch.

        Args:
            batch_in: The batch.

        Returns:
            The loss of the given batch.
        """
        raise NotImplementedError

    def batch_predict(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> Dict[Text, tf.Tensor]:
        """Predicts the output of the given batch.

        Args:
            batch_in: The batch.

        Returns:
            The output to predict.
        """
        raise NotImplementedError
