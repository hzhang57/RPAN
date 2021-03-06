import tensorflow as tf 
import util as util
import resnet 

class Graph():
  def __init__( self, T, C ):

    self.T          = T
    self.C          = C

    self.X          = tf.placeholder( tf.float32, [None, self.T, 224, 224, 3] )
    self.Y          = tf.placeholder( tf.int32,   [None, self.T] )
    
    # The Joint annotations.
    self.J          = 18                            # Using CMU Openpose
    self.P          = tf.placeholder( tf.float32, [None, self.T, 7, 7, self.J] )

    self.LR         = tf.placeholder( tf.float32 )  # Learning rate
    self.lambda_l2  = tf.placeholder( tf.float32 )  # Regularization factor
    self.phase      = tf.placeholder( tf.bool )     # Training phase

    self.BATCH      = tf.shape( self.X )[0]
    self.BT         = self.BATCH * self.T
    self.scope      = "Model"                       # Train only variables in scope

    self.l_action   = 1.0
    self.l_pose     = 1.0

    self.DIM_LSTM   = 512                           # Dimensionality of LSTM
    self.DIM_ATT    = 32                            # Either 32 (Sub-JHMDB) or 128 (PennAction)

    # Init ResNet
    self.net        = resnet.ResNet()               # We are using ResNet as base DCN. Change here.
    self.net.phase  = self.phase

  # Generate one set of att. maps for each of 5 body parts
  def generate_attention_maps( self, state, feature ):

    h, c  = state
    DIM   = self.DIM_ATT
    
    # There are 5 body parts. `tmp` is shared for each joint within a body part.
    # In other words, we need 5 `tmp` terms, or equivalently, 1 `tmp` term with 5*DIM channels. 
    
    # Compute map (Eq. 2)
    Ac    = util.conv2d( feature, [1, 1, 5*DIM], "att_pose_c", use_bias = False )
    Ah    = util.fc( h, 5*DIM, "att_pose_h", use_bias = False )
    bias  = tf.get_variable( "bias", shape = [5*DIM], initializer=tf.zeros_initializer() )

    # A_c: Bx7x7x32; A_h: Bx32.
    # Add A_h to A_c by broadcasting
    tmp   = tf.nn.tanh( tf.reshape( Ah, [self.BATCH, 1, 1, DIM] ) + Ac + bias )
    tmp   = tf.split( tmp, 5, axis=3 )  # Split into 5 groups

    joint_maps = []
    joint_tens = []
    for i in range( 5 ):
      # v is just a 1x1 convolution.
      # NOTE: From paper, it is not entirely clear if v is shared between body parts.
      # We assume this is NOT the case.
      res   = util.conv2d( tmp[i], [1, 1, self.J], "att_map_bp" + str(i) )
      res   = tf.reshape( res, [self.BATCH, 7, 7, self.J] )

      # Normalization (Eq. 3)
      t_res = tf.nn.softmax( res, 3 )

      l_res = tf.split( t_res, self.J, axis=3 )

      joint_maps.append( l_res )                            # For use in assemble_parts
      joint_tens.append( tf.expand_dims( t_res, axis=1 ) )  # For convenient loss computation

    joint_tens = tf.concat( joint_tens, axis=1 )  # Resulting shape: BATCH x 5 x 7 x 7 x J
    return joint_maps, joint_tens


  # Body Parts (Joint indices) on CMU:
  # Torso (0, 1, 2, 4, 8, 11, 14, 15, 16, 17)
  # Elbow (3, 6)
  # Wrist (4, 7)
  # Knee  (9, 12)
  # Ankle (10, 13)

  def assemble_parts( self, joint_maps, feature ):

    h_torso        = feature * joint_maps[0][0]
    h_torso       += feature * joint_maps[0][1]
    h_torso       += feature * joint_maps[0][2]
    h_torso       += feature * joint_maps[0][4]
    h_torso       += feature * joint_maps[0][8]
    h_torso       += feature * joint_maps[0][11]
    h_torso       += feature * joint_maps[0][14]
    h_torso       += feature * joint_maps[0][15]
    h_torso       += feature * joint_maps[0][16]
    h_torso       += feature * joint_maps[0][17]

    h_elbow        = feature * joint_maps[1][3]
    h_elbow       += feature * joint_maps[1][6]

    h_wrist        = feature * joint_maps[2][4]
    h_wrist       += feature * joint_maps[2][7]

    h_knee         = feature * joint_maps[3][9]
    h_knee        += feature * joint_maps[3][12]

    h_ankle        = feature * joint_maps[4][10]
    h_ankle       += feature * joint_maps[4][13]

    h_parts = [ tf.expand_dims( h_torso, 1 ), tf.expand_dims( h_elbow, 1 ), tf.expand_dims( h_wrist, 1 ), tf.expand_dims( h_knee, 1 ), tf.expand_dims( h_ankle, 1 ) ]
    return tf.concat( h_parts, axis=1 )


  def build_graph( self ):
    # Extract DCN features (here ResNet v2, 50 layers)
    X           = tf.reshape( self.X, [self.BT, 224, 224, 3] )
    _           = self.net.resnet_v2( X )


    features    = tf.reshape( self.net.spatial, [self.BATCH, self.T, 7, 7, 2048] )
    self.features = features

    # Encoder
    with tf.variable_scope( self.scope ):
      with tf.variable_scope( "LSTM2" ) as scope:
        lstm  = tf.contrib.rnn.LSTMCell( self.DIM_LSTM, initializer=tf.contrib.layers.xavier_initializer() )
        state = lstm.zero_state( self.BATCH, tf.float32 )


        feat_T    = tf.split( features, self.T, axis=1 )

        outputs = []
        joint_maps = []
        for t in range( self.T ):
          # TODO: Each body part has its own variables
          if t > 0:
            scope.reuse_variables()

          # Generate Attention Map for each Joint and normalize
          h_rgb = tf.reshape( feat_T[t], [self.BATCH, 7, 7, 2048] )
          jm_list, jm_tensor  = self.generate_attention_maps( state, h_rgb )
          joint_maps.append( tf.expand_dims( jm_tensor, axis=2 ) )  # B x 5 x T x 7 x 7 x J

          # Assemble Parts
          body_parts  = self.assemble_parts( jm_list, h_rgb )   # F_t^P
          body_pooled = tf.reduce_max( body_parts, axis=1 )     # S_t

          # body_pooled = tf.reshape( body_pooled, [self.BATCH, 7*7*2048] )
          # Global pooling to save resources
          body_pooled   = tf.reduce_mean( body_pooled, axis=[1,2] )

          feat_out, state = lstm( body_pooled, state )

          outputs.append( tf.expand_dims( feat_out, axis=1 ) )


      h_lstm = tf.concat( outputs, axis=1 )
      h_lstm = tf.reshape( h_lstm, [self.BT, self.DIM_LSTM] )

      h_pred = util.fc( h_lstm, self.C, "classifier_pose" )
      h_pred = tf.reshape( h_pred, [self.BATCH, self.T, self.C] )

    # Loss computation
    var_list = tf.get_collection( tf.GraphKeys.TRAINABLE_VARIABLES, scope = self.scope )
    reg_loss = tf.get_collection( tf.GraphKeys.REGULARIZATION_LOSSES, scope = self.scope )

    # Main losses: Softmax classification loss
    loss_pose_pre = tf.nn.sparse_softmax_cross_entropy_with_logits( logits = h_pred, labels = self.Y )
    loss_pose_T   = loss_pose_pre
    loss_pose_cls = tf.reduce_sum( loss_pose_pre, axis=1 )

    # Main losses: Joint map L2 regression loss
    joint_maps    = tf.concat( joint_maps, axis=2 )
    loss_pose_l2  = 0

    # Note, we got 5 sets of attention maps. Each have an L2 loss.
    for i in range( 5 ):
      diff          = tf.reshape( joint_maps[:,i] - self.P, [self.BATCH, self.T, 7*7*self.J] )
      loss_pose_l2 += 0.5 * tf.reduce_sum( diff ** 2, axis=2 )

    # Total Loss
    loss     = tf.reduce_mean(    self.l_action * loss_pose_pre
                                + self.l_pose   * loss_pose_l2 )

    reg_loss = self.lambda_l2 * tf.reduce_sum( reg_loss )
    total    = reg_loss + loss

    # Optimizer + Batch Gradient Accumulation
    #opt         = tf.train.RMSPropOptimizer( learning_rate = self.LR )
    opt         = tf.train.AdamOptimizer( learning_rate = self.LR )

    accum_vars  = [tf.Variable( tf.zeros_like( tv.initialized_value() ), trainable = False ) for tv in var_list]
    zero_ops    = [tv.assign( tf.zeros_like( tv ) ) for tv in accum_vars] 

    gvs         = opt.compute_gradients( total, var_list )

    accum_ops   = [accum_vars[i].assign_add(gv[0]) for i, gv in enumerate( gvs )]
    op          = opt.apply_gradients( [(accum_vars[i], gv[1]) for i, gv in enumerate(gvs)] )

    # Exposing variables
    self.joint_maps = joint_maps
    self.reg_loss   = reg_loss
    self.loss_main_T= loss_pose_T
    self.loss_rpan  = loss_pose_cls
    self.loss_pose  = loss_pose_l2
    self.zero_ops   = zero_ops
    self.accum_ops  = accum_ops
    self.accum_vars = accum_vars

    self.result     = tf.nn.softmax( h_pred )

    return op, total
