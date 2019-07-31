### BEGIN code added by notebook conversion ###
import logging
### END code added by notebook conversion ###

### BEGIN Cell #1 ###
import os, json, math, shutil
import datetime
import numpy as np
import tensorflow as tf
logging.info(tf.__version__)
### END Cell #1 ###

### BEGIN PARAMS from notebook ###
# Note that this cell is special. It's got a tag (you can view tags by clicking on the wrench icon on the left menu in Jupyter)
# These are parameters that we will configure so that we can schedule this notebook
DATADIR = '../data'
OUTDIR = './trained_model'
EXPORT_DIR = os.path.join(OUTDIR,'export/savedmodel')
NBUCKETS = 10  # for feature crossing
TRAIN_BATCH_SIZE = 32
NUM_TRAIN_EXAMPLES = 10000 * 5 # remember the training dataset repeats, so this will wrap around
NUM_EVALS = 5  # evaluate this many times
NUM_EVAL_EXAMPLES = 10000 # enough to get a reasonable sample, but no so much that it slows down
### END PARAMS from notebook ###

### BEGIN PARAMS from YAML ###
NBUCKETS = 10
NUM_TRAIN_EXAMPLES = 5000000
OUTDIR = "gs://cloud-training-demos-ml/quests/serverlessml/"
NUM_EVALS = 5
EXPORT_DIR = "gs://cloud-training-demos-ml/quests/serverlessml/export/savedmodel"
TRAIN_BATCH_SIZE = 32
NUM_EVAL_EXAMPLES = 100000
DATADIR = "gs://cloud-training-demos-ml/quests/serverlessml/data"
### END PARAMS from YAML ###

### BEGIN Cell #4 ###
CSV_COLUMNS  = ['fare_amount',  'pickup_datetime',
                'pickup_longitude', 'pickup_latitude', 
                'dropoff_longitude', 'dropoff_latitude', 
                'passenger_count', 'key']
LABEL_COLUMN = 'fare_amount'
DEFAULTS     = [[0.0],['na'],[0.0],[0.0],[0.0],[0.0],[0.0],['na']]
### END Cell #4 ###

### BEGIN Cell #5 ###
def features_and_labels(row_data):
    for unwanted_col in ['key']:  # keep the pickup_datetime!
        row_data.pop(unwanted_col)
    label = row_data.pop(LABEL_COLUMN)
    return row_data, label  # features, label

# load the training data
def load_dataset(pattern, batch_size=1, mode=tf.estimator.ModeKeys.EVAL):
    pattern = '{}/{}'.format(DATADIR, pattern)
    dataset = (tf.data.experimental.make_csv_dataset(pattern, batch_size, CSV_COLUMNS, DEFAULTS)
               .map(features_and_labels) # features, label
               .cache())
    if mode == tf.estimator.ModeKeys.TRAIN:
        logging.info("Repeating training dataset indefinitely")
        dataset = dataset.shuffle(1000).repeat()
    dataset = dataset.prefetch(1) # take advantage of multi-threading; 1=AUTOTUNE
    return dataset
### END Cell #5 ###

### BEGIN Cell #21 ###
def parse_datetime(s):
    if type(s) is not str:
        s = s.numpy().decode('utf-8') # if it is a Tensor
    try:
        # Python 3.5 doesn't handle timezones of the form 00:00, only 0000
        return datetime.datetime.strptime(s.replace(':',''), "%Y-%m-%d %H%M%S%z")
    except:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S %Z")
### END Cell #21 ###

### BEGIN Cell #8 ###
## Add transformations
def euclidean(params):
    lon1, lat1, lon2, lat2 = params
    londiff = lon2 - lon1
    latdiff = lat2 - lat1
    return tf.sqrt(londiff*londiff + latdiff*latdiff)

DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
def get_dayofweek(s):
    ts = parse_datetime(s)
    return DAYS[ts.weekday()]

def dayofweek(ts_in):
    return tf.map_fn(
        lambda s: tf.py_function(get_dayofweek, inp=[s], Tout=tf.string),
        ts_in
    )

def transform(inputs, NUMERIC_COLS, STRING_COLS):
    transformed = inputs.copy()
    logging.info("BEFORE TRANSFORMATION")
    logging.info("INPUTS:", inputs.keys())
    logging.info(inputs['pickup_longitude'].shape)
    feature_columns = {
        colname: tf.feature_column.numeric_column(colname)
           for colname in NUMERIC_COLS
    }
    
    # scale the lat, lon values to be in 0, 1
    for lon_col in ['pickup_longitude', 'dropoff_longitude']:  # in range -70 to -78
        transformed[lon_col] = tf.keras.layers.Lambda(
            lambda x: (x+78)/8.0, 
            name='scale_{}'.format(lon_col)
        )(inputs[lon_col])
    for lat_col in ['pickup_latitude', 'dropoff_latitude']: # in range 37 to 45
        transformed[lat_col] = tf.keras.layers.Lambda(
            lambda x: (x-37)/8.0, 
            name='scale_{}'.format(lat_col)
        )(inputs[lat_col])

    # add Euclidean distance. Doesn't have to be accurate calculation because NN will calibrate it
    transformed['euclidean'] = tf.keras.layers.Lambda(euclidean, name='euclidean')([
        inputs['pickup_longitude'],
        inputs['pickup_latitude'],
        inputs['dropoff_longitude'],
        inputs['dropoff_latitude']
    ])
    feature_columns['euclidean'] = tf.feature_column.numeric_column('euclidean')
    
    # hour of day from timestamp of form '2010-02-08 09:17:00+00:00'
    transformed['hourofday'] = tf.keras.layers.Lambda(
        lambda x: tf.strings.to_number(tf.strings.substr(x, 11, 2), out_type=tf.dtypes.int32),
        name='hourofday'
    )(inputs['pickup_datetime'])
    feature_columns['hourofday'] = tf.feature_column.indicator_column(
        tf.feature_column.categorical_column_with_identity('hourofday', num_buckets=24))


    # day of week is hard because there is no TensorFlow function for date handling
    transformed['dayofweek'] = tf.keras.layers.Lambda(
        lambda x: dayofweek(x),
        name='dayofweek_pyfun'
    )(inputs['pickup_datetime'])
    transformed['dayofweek'] = tf.keras.layers.Reshape((), name='dayofweek')(transformed['dayofweek'])
    feature_columns['dayofweek'] = tf.feature_column.indicator_column(
        tf.feature_column.categorical_column_with_vocabulary_list(
          'dayofweek', vocabulary_list = DAYS))
    
    # featurecross lat, lon into nxn buckets, then embed
    # b/135479527
    #nbuckets = NBUCKETS
    #latbuckets = np.linspace(0, 1, nbuckets).tolist()
    #lonbuckets = np.linspace(0, 1, nbuckets).tolist()
    #b_plat = tf.feature_column.bucketized_column(feature_columns['pickup_latitude'], latbuckets)
    #b_dlat = tf.feature_column.bucketized_column(feature_columns['dropoff_latitude'], latbuckets)
    #b_plon = tf.feature_column.bucketized_column(feature_columns['pickup_longitude'], lonbuckets)
    #b_dlon = tf.feature_column.bucketized_column(feature_columns['dropoff_longitude'], lonbuckets)
    #ploc = tf.feature_column.crossed_column([b_plat, b_plon], nbuckets * nbuckets)
    #dloc = tf.feature_column.crossed_column([b_dlat, b_dlon], nbuckets * nbuckets)
    #pd_pair = tf.feature_column.crossed_column([ploc, dloc], nbuckets ** 4 )
    #feature_columns['pickup_and_dropoff'] = tf.feature_column.embedding_column(pd_pair, 100)

    logging.info("AFTER TRANSFORMATION")
    logging.info("TRANSFORMED:", transformed.keys())
    logging.info("FEATURES", feature_columns.keys())   
    return transformed, feature_columns

def rmse(y_true, y_pred):
    return tf.sqrt(tf.reduce_mean(tf.square(y_pred - y_true))) 

def build_dnn_model():
    # input layer is all float except for pickup_datetime which is a string
    STRING_COLS = ['pickup_datetime']
    NUMERIC_COLS = set(CSV_COLUMNS) - set([LABEL_COLUMN, 'key']) - set(STRING_COLS)
    logging.info(STRING_COLS)
    logging.info(NUMERIC_COLS)
    inputs = {
        colname : tf.keras.layers.Input(name=colname, shape=(), dtype='float32')
           for colname in NUMERIC_COLS
    }
    inputs.update({
        colname : tf.keras.layers.Input(name=colname, shape=(), dtype='string')
           for colname in STRING_COLS
    })
    
    # transforms
    transformed, feature_columns = transform(inputs, NUMERIC_COLS, STRING_COLS)
    dnn_inputs = tf.keras.layers.DenseFeatures(feature_columns.values())(transformed)

    # two hidden layers of [32, 8] just in like the BQML DNN
    h1 = tf.keras.layers.Dense(32, activation='relu', name='h1')(dnn_inputs)
    h2 = tf.keras.layers.Dense(8, activation='relu', name='h2')(h1)

    # final output would normally have a linear activation because this is regression
    # However, we know something about the taxi problem (fares are +ve and tend to be below $60).
    # Use that here. (You can verify by running this query):
    # SELECT APPROX_QUANTILES(fare_amount, 100) FROM serverlessml.cleaned_training_data
    # b/136476088
    #fare_thresh = lambda x: 60 * tf.keras.activations.relu(x)
    #output = tf.keras.layers.Dense(1, activation=fare_thresh, name='fare')(h2)
    output = tf.keras.layers.Dense(1, name='fare')(h2)
    
    model = tf.keras.models.Model(inputs, output)
    model.compile(optimizer='adam', loss='mse', metrics=[rmse, 'mse'])
    return model

model = build_dnn_model()
logging.info(model.summary())
### END Cell #8 ###

### BEGIN Cell #10 ###
trainds = load_dataset('taxi-train*', TRAIN_BATCH_SIZE, tf.estimator.ModeKeys.TRAIN)
evalds = load_dataset('taxi-valid*', 1000, tf.estimator.ModeKeys.EVAL).take(NUM_EVAL_EXAMPLES//10000) # evaluate on 1/10 final evaluation set

steps_per_epoch = NUM_TRAIN_EXAMPLES // (TRAIN_BATCH_SIZE * NUM_EVALS)

shutil.rmtree('{}/checkpoints/'.format(OUTDIR), ignore_errors=True)
checkpoint_path = '{}/checkpoints/taxi'.format(OUTDIR)
cp_callback = tf.keras.callbacks.ModelCheckpoint(checkpoint_path, 
                                                 save_weights_only=True,
                                                 verbose=1)

history = model.fit(trainds, 
                    validation_data=evalds,
                    epochs=NUM_EVALS, 
                    steps_per_epoch=steps_per_epoch,
                    callbacks=[cp_callback])
### END Cell #10 ###

### BEGIN Cell #12 ###
evalds = load_dataset('taxi-valid*', 1000, tf.estimator.ModeKeys.EVAL).take(NUM_EVAL_EXAMPLES//1000)
model.evaluate(evalds)
### END Cell #12 ###

### BEGIN Cell #14 ###
export_dir = os.path.join(EXPORT_DIR, datetime.datetime.now().strftime('%Y%m%d%H%M%S'))
tf.keras.experimental.export_saved_model(model, export_dir)
logging.info(export_dir)

# Recreate the exact same model
new_model = tf.keras.experimental.load_from_saved_model(export_dir)

# try predicting with this model
new_model.predict({
    'pickup_longitude': tf.convert_to_tensor([-73.982683]),
    'pickup_latitude': tf.convert_to_tensor([40.742104]),
    'dropoff_longitude': tf.convert_to_tensor([-73.983766]),
    'dropoff_latitude': tf.convert_to_tensor([40.755174]),
    'passenger_count': tf.convert_to_tensor([3.0]),  
    'pickup_datetime': tf.convert_to_tensor(['2010-02-08 09:17:00+00:00'], dtype=tf.string),
})
### END Cell #14 ###

